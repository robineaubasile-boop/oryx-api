// R1-C3 — Décrypter : conversation_key propre à l'onglet (sessionStorage,
// R1-C3A) et contexte texte filtré par ticker (R1-C3B). L'outbox d'ACK reste
// en localStorage, partagée entre onglets ; Éducation / Coach / Checklist
// inchangés.
//
// Deux onglets = deux fenêtres jsdom avec chacune son sessionStorage, mais un
// localStorage PARTAGÉ (stub en mémoire, même origine), comme un navigateur.
// Limite V1 documentée, non testée : la duplication native d'un onglet peut,
// selon le navigateur, copier le sessionStorage initial.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const HTML = fs.readFileSync(path.join(__dirname, '../../web-v2/index.html'), 'utf8');
const OUTBOX_KEY = 'oryx_pending_delivery_acks_v1';
const SESSION_KEY = 'oryx_v2_session_decrypter';
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// Résolution canonique simulée du backend (ex. LVMH -> MC.PA).
const CANONICAL = { LVMH: 'MC.PA', 'MC.PA': 'MC.PA', NVIDIA: 'NVDA', NVDA: 'NVDA', AAPL: 'AAPL' };

function response(body, { ok = true, status = 200 } = {}) {
	return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
}

async function waitFor(fn, tries = 200) {
	for (let i = 0; i < tries; i++) {
		const result = fn();
		if (result) return result;
		await new Promise(r => setTimeout(r, 5));
	}
	return null;
}

const settle = () => new Promise(r => setTimeout(r, 50));

// localStorage partagé par tous les onglets d'une même origine.
function sharedLocalStorage(seed = {}) {
	const data = new Map(Object.entries(seed));
	return {
		data,
		getItem: (k) => (data.has(k) ? data.get(k) : null),
		setItem: (k, v) => { data.set(k, String(v)); },
		removeItem: (k) => { data.delete(k); },
		clear: () => data.clear(),
		key: (i) => [...data.keys()][i] ?? null,
		get length() { return data.size; },
	};
}

let turnCounter = 0;
function nextTurnId() {
	turnCounter += 1;
	return `00000000-0000-4000-8000-${String(turnCounter).padStart(12, '0')}`;
}

// Réponse /decryptage : ticker canonique, texte qui nomme le tour.
function decryptageOk(call) {
	const ticker = CANONICAL[call.body.ticker] || call.body.ticker;
	return response({
		success: true, ticker, name: ticker, method_used: 'construction_these',
		analysis: `Réponse ${ticker} à « ${call.body.question} »`, price: 1, currency: 'EUR', disclaimer: 'd',
		assistant_turn_id: nextTurnId(), delivery_status: 'pending',
	});
}

function ackOk(call) {
	const turn = decodeURIComponent(call.url.split('/')[4]);
	return response({ success: true, assistant_turn_id: turn, delivery_status: 'delivered' });
}

// Ouvre un onglet web-v2. `local` = localStorage partagé ; `session` = seed du
// sessionStorage de CET onglet (rechargement).
function openTab({ local = sharedLocalStorage(), session = {}, handlers = {} } = {}) {
	const calls = [];
	function mockFetch(url, options = {}) {
		const call = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null };
		calls.push(call);
		if (url === '/decryptage') return (handlers.decryptage || decryptageOk)(call);
		if (url.startsWith('/api/runtime/assistant-deliveries/')) return (handlers.ack || ackOk)(call);
		if (url === '/checklist') return response({ success: true, ticker: call.body.ticker, analysis: `Checklist ${call.body.ticker}` });
		if (url === '/api/user/test-user' && call.method === 'GET') return response({ level: 'debutant', portfolio: [] });
		if (url.endsWith('/theses')) return (handlers.theses || (() => response([])))(call);
		return response({});
	}
	local.setItem('oryx_user_id', 'test-user');
	local.setItem('oryx_migrated_v1', 'true');
	const dom = new JSDOM(HTML, {
		url: 'http://localhost/web-v2/',
		runScripts: 'dangerously',
		pretendToBeVisual: true,
		beforeParse(window) {
			window.fetch = mockFetch;
			Object.defineProperty(window, 'localStorage', { value: local, configurable: true });
			for (const [k, v] of Object.entries(session)) window.sessionStorage.setItem(k, v);
		},
	});
	const { document, sessionStorage } = dom.window;
	const of = (prefix) => calls.filter(c => c.url.startsWith(prefix));
	return {
		dom, document, sessionStorage, local, calls,
		decryptageCalls: () => of('/decryptage'),
		ackCalls: () => of('/api/runtime/assistant-deliveries/'),
		outbox: () => JSON.parse(local.getItem(OUTBOX_KEY) || '[]'),
		lastBody: () => of('/decryptage').slice(-1)[0].body,
		async send(question, ticker, key = 'decrypter') {
			document.getElementById('ticker-' + key).value = ticker;
			document.getElementById('input-' + key).value = question;
			document.getElementById('send-' + key).click();
			await settle();
			await waitFor(() => !document.getElementById('send-' + key).disabled);
			await settle();
		},
		async newConversation() {
			document.getElementById('new-convo-decrypter').click();
			await settle();
		},
		close() { dom.window.close(); },
	};
}

// --- R1-C3A : conversation_key par onglet ------------------------------------

test('A. deux onglets normaux : deux conversation_key ; reset de B ne touche pas A', async () => {
	const local = sharedLocalStorage();
	const a = openTab({ local });
	const b = openTab({ local });
	await a.send('Analyse LVMH', 'LVMH');
	await b.send('Analyse NVIDIA', 'NVDA');
	const keyA = a.lastBody().conversation_key;
	const keyB = b.lastBody().conversation_key;
	assert.match(keyA, UUID_RE);
	assert.match(keyB, UUID_RE);
	assert.notStrictEqual(keyA, keyB);

	await waitFor(() => b.outbox().length === 0);
	await b.newConversation();
	const keyB2 = b.sessionStorage.getItem(SESSION_KEY);
	assert.match(keyB2, UUID_RE);
	assert.notStrictEqual(keyB2, keyB);

	// A envoie APRÈS la nouvelle conversation de B : il garde SA clé.
	await a.send('Et la marque ?', 'LVMH');
	assert.strictEqual(a.lastBody().conversation_key, keyA);
	assert.strictEqual(a.sessionStorage.getItem(SESSION_KEY), keyA);
	await b.send('Et les GPU ?', 'NVDA');
	assert.strictEqual(b.lastBody().conversation_key, keyB2);
	// La clé Décrypter n'est jamais écrite dans le stockage partagé.
	assert.strictEqual(local.getItem(SESSION_KEY), null);
	a.close();
	b.close();
});

test('A bis. une ancienne clé Décrypter partagée en localStorage est ignorée', async () => {
	const local = sharedLocalStorage({ [SESSION_KEY]: 'legacy-shared-key' });
	const a = openTab({ local });
	const b = openTab({ local });
	await a.send('Analyse LVMH', 'LVMH');
	await b.send('Analyse NVIDIA', 'NVDA');
	const [keyA, keyB] = [a.lastBody().conversation_key, b.lastBody().conversation_key];
	assert.notStrictEqual(keyA, 'legacy-shared-key');
	assert.notStrictEqual(keyB, 'legacy-shared-key');
	assert.notStrictEqual(keyA, keyB);
	a.close();
	b.close();
});

test('B. rechargement du même onglet : même sessionStorage => même conversation_key', async () => {
	const local = sharedLocalStorage();
	const first = openTab({ local });
	await first.send('Analyse LVMH', 'LVMH');
	const key = first.lastBody().conversation_key;
	const sessionSnapshot = { [SESSION_KEY]: first.sessionStorage.getItem(SESSION_KEY) };
	first.close();

	const reloaded = openTab({ local, session: sessionSnapshot });
	await reloaded.send('Suite', 'LVMH');
	assert.strictEqual(reloaded.lastBody().conversation_key, key);
	reloaded.close();
});

test('C. Nouvelle conversation : seule la clé Décrypter de l\'onglet (sessionStorage) change', async () => {
	const local = sharedLocalStorage();
	const tab = openTab({ local });
	await tab.send('Analyse LVMH', 'LVMH');
	await waitFor(() => tab.outbox().length === 0);
	const before = tab.sessionStorage.getItem(SESSION_KEY);
	const sharedBefore = JSON.stringify([...local.data.entries()]);
	await tab.newConversation();
	const after = tab.sessionStorage.getItem(SESSION_KEY);
	assert.match(after, UUID_RE);
	assert.notStrictEqual(after, before);
	assert.strictEqual(JSON.stringify([...local.data.entries()]), sharedBefore, 'localStorage partagé inchangé');
	tab.close();
});

test('D. outbox d\'ACK toujours en localStorage, rejouée par un autre onglet (R1-C1/R1-C2 inchangés)', async () => {
	const local = sharedLocalStorage();
	const a = openTab({ local, handlers: { ack: () => Promise.reject(new TypeError('offline')) } });
	await a.send('Analyse LVMH', 'LVMH');
	await waitFor(() => a.ackCalls().length >= 1);
	await settle();
	const keyA = a.lastBody().conversation_key;
	const [entry] = a.outbox();
	assert.strictEqual(entry.conversation_key, keyA);
	assert.strictEqual(entry.surface, 'decryptage');
	assert.strictEqual(a.sessionStorage.getItem(OUTBOX_KEY), null, 'jamais en sessionStorage');
	a.close();

	// Onglet B : flush obligatoire avant envoi => l'ACK de A est rejoué.
	const b = openTab({ local });
	await b.send('Analyse NVIDIA', 'NVDA');
	const replayed = b.ackCalls().find(c => c.url.includes(entry.assistant_turn_id));
	assert.ok(replayed, 'ACK de l\'onglet A rejoué depuis B');
	assert.deepStrictEqual(replayed.body, { user_id: 'test-user', conversation_key: keyA });
	assert.notStrictEqual(b.lastBody().conversation_key, keyA);
	await waitFor(() => b.outbox().length === 0);
	assert.deepStrictEqual(b.outbox(), []);
	b.close();
});

test('E. Éducation / Coach : session_id inchangé, partagé en localStorage', async () => {
	const local = sharedLocalStorage();
	const a = openTab({ local });
	const b = openTab({ local });
	for (const view of ['education', 'coach']) {
		const key = 'oryx_v2_session_' + view;
		assert.match(local.getItem(key), UUID_RE);
		assert.strictEqual(a.sessionStorage.getItem(key), null);
		assert.strictEqual(b.sessionStorage.getItem(key), null);
	}
	a.close();
	b.close();
});

test('F. « Reprendre » : nouvelle conversation_key dans l\'onglet courant uniquement', async () => {
	const local = sharedLocalStorage();
	const theses = () => response([{ ticker: 'NVDA', current_step: 'moat', updated_at: '2026-10-04T10:00:00+00:00', theses: [] }]);
	const a = openTab({ local, handlers: { theses } });
	const b = openTab({ local, handlers: { theses } });
	await a.send('Analyse LVMH', 'LVMH');
	await b.send('Analyse NVIDIA', 'NVDA');
	const keyA = a.lastBody().conversation_key;
	const keyB = b.lastBody().conversation_key;
	await waitFor(() => b.outbox().length === 0);

	const resume = await waitFor(() => b.document.querySelector('.thesis-resume'));
	resume.click();
	await waitFor(() => b.decryptageCalls().length === 2);
	await settle();
	const body = b.lastBody();
	assert.notStrictEqual(body.conversation_key, keyB);
	assert.strictEqual(body.conversation_key, b.sessionStorage.getItem(SESSION_KEY));
	assert.strictEqual(body.context, '');

	await a.send('Suite LVMH', 'LVMH');
	assert.strictEqual(a.lastBody().conversation_key, keyA);
	a.close();
	b.close();
});

// --- R1-C3B : contexte Décrypter filtré par ticker -----------------------------

function contextLines(context) {
	return context ? context.split('\n') : [];
}

test('contexte : LVMH / NVDA / LVMH => chaque ticker ne voit que ses propres tours, dans l\'ordre', async () => {
	const tab = openTab();
	await tab.send('L1', 'LVMH');
	await tab.send('N1', 'NVDA');
	await tab.send('L2', 'LVMH');

	await tab.send('N2', 'NVDA');
	assert.deepStrictEqual(contextLines(tab.lastBody().context), [
		'[ASSISTANT] Réponse NVDA à « N1 »',
		'[USER] N1',
	]);

	await tab.send('L3', 'LVMH');
	assert.deepStrictEqual(contextLines(tab.lastBody().context), [
		'[ASSISTANT] Réponse MC.PA à « L2 »',
		'[USER] L2',
		'[ASSISTANT] Réponse MC.PA à « L1 »',
		'[USER] L1',
	]);
	tab.close();
});

test('contexte : premier passage sur un ticker => aucun historique d\'un autre ticker', async () => {
	const tab = openTab();
	await tab.send('L1', 'LVMH');
	await tab.send('Passons à NVIDIA', 'NVDA');
	const body = tab.decryptageCalls()[1].body;
	assert.strictEqual(body.ticker, 'NVDA');
	assert.strictEqual(body.context, '');
	tab.close();
});

test('contexte : entrées enregistrées avec le ticker canonique de la réponse (LVMH -> MC.PA)', async () => {
	const tab = openTab();
	await tab.send('L1', 'LVMH');
	await tab.send('L2', 'MC.PA');
	assert.deepStrictEqual(contextLines(tab.lastBody().context), ['[ASSISTANT] Réponse MC.PA à « L1 »', '[USER] L1']);
	await tab.send('L3', 'LVMH');
	assert.strictEqual(contextLines(tab.lastBody().context).length, 4);
	assert.ok(contextLines(tab.lastBody().context).every(l => !l.includes('NVDA')));
	tab.close();
});

test('contexte : maxHistoryTurns (20) appliqué APRÈS le filtrage par ticker', async () => {
	const tab = openTab();
	for (let i = 1; i <= 11; i++) {
		await tab.send(`L${i}`, 'LVMH');
		await tab.send(`N${i}`, 'NVDA');
	}
	await tab.send('L12', 'LVMH');
	const lines = contextLines(tab.lastBody().context);
	assert.strictEqual(lines.length, 20);
	assert.ok(lines.every(l => l.includes('L') && !l.includes('NVDA') && !/\bN\d/.test(l)));
	// Les 10 derniers tours LVMH (L2..L11), du plus récent au plus ancien.
	assert.strictEqual(lines[0], '[ASSISTANT] Réponse MC.PA à « L11 »');
	assert.strictEqual(lines[19], '[USER] L2');
	tab.close();
});

test('contexte : Nouvelle conversation vide aussi l\'historique par ticker', async () => {
	const tab = openTab();
	await tab.send('L1', 'LVMH');
	await waitFor(() => tab.outbox().length === 0);
	await tab.newConversation();
	await tab.send('L2', 'LVMH');
	assert.strictEqual(tab.lastBody().context, '');
	tab.close();
});

test('Checklist non impactée : contexte historique non filtré par ticker', async () => {
	const tab = openTab();
	await tab.send('Q1', 'AAPL', 'checklist');
	await tab.send('Q2', 'MSFT', 'checklist');
	const [, second] = tab.calls.filter(c => c.url === '/checklist');
	assert.deepStrictEqual(second.body, {
		ticker: 'MSFT', question: 'Q2', context: '[ASSISTANT] Checklist AAPL\n[USER] Q1', level: 'debutant', user_id: 'test-user',
	});
	tab.close();
});
