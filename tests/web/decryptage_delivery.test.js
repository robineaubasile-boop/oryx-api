// R1-C1 — Décrypter : identités de tour, retry réseau idempotent, ACK de
// livraison après rendu, outbox localStorage, flush obligatoire avant un
// nouvel envoi / une nouvelle conversation, userReady. Checklist inchangée.
// R1-C3A : la conversation_key Décrypter est lue en sessionStorage (propre
// à l'onglet) ; l'outbox d'ACK reste en localStorage (cross-tab).
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const HTML = fs.readFileSync(path.join(__dirname, '../../web-v2/index.html'), 'utf8');
const OUTBOX_KEY = 'oryx_pending_delivery_acks_v1';
const SESSION_KEY = 'oryx_v2_session_decrypter';
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ANALYSIS = 'Analyse pédagogique de LVMH.';

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

// ACK 2xx valide : renvoie le même assistant_turn_id, statut delivered.
function ackOk(call) {
	const turn = decodeURIComponent(call.url.split('/')[4]);
	return response({ success: true, assistant_turn_id: turn, delivery_status: 'delivered' });
}

function decryptageSuccess(turn, extra = {}) {
	return {
		success: true, ticker: 'MC.PA', name: 'LVMH', method_used: 'construction_these', analysis: ANALYSIS,
		price: 600, currency: 'EUR', disclaimer: 'Analyse éducative uniquement.',
		assistant_turn_id: turn, delivery_status: 'pending', ...extra,
	};
}

// Monte web-v2 avec un fetch simulé. handlers.decryptage / ack / user /
// checklist / theses reçoivent (url, options, document) ; tous les appels
// sont enregistrés dans `calls`.
function mount({ handlers = {}, storage = {}, migrated = true } = {}) {
	const calls = [];
	const outboxWrites = [];
	let dom;
	function mockFetch(url, options = {}) {
		const call = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null };
		calls.push(call);
		const document = dom && dom.window.document;
		if (url === '/decryptage') return (handlers.decryptage || (() => response(decryptageSuccess('11111111-1111-4111-8111-111111111111'))))(call, document);
		if (url.startsWith('/api/runtime/assistant-deliveries/')) return (handlers.ack || ackOk)(call, document);
		if (url === '/checklist') return (handlers.checklist || (() => response({ success: true, ticker: 'AAPL', analysis: 'Checklist.' })))(call, document);
		if (url === '/api/user/test-user' && call.method === 'GET') return (handlers.user || (() => response({ level: 'debutant', portfolio: [] })))(call, document);
		if (url.endsWith('/theses')) return (handlers.theses || (() => response([])))(call, document);
		return response({});
	}
	dom = new JSDOM(HTML, {
		url: 'http://localhost/web-v2/',
		runScripts: 'dangerously',
		pretendToBeVisual: true,
		beforeParse(window) {
			window.fetch = mockFetch;
			// Instantané du DOM à chaque écriture de l'outbox d'ACK.
			const setItem = window.Storage.prototype.setItem;
			window.Storage.prototype.setItem = function (k, v) {
				if (k === OUTBOX_KEY) outboxWrites.push({ assistantRendered: window.document.querySelectorAll('#msgs-decrypter .msg.assistant').length, value: v });
				return setItem.call(this, k, v);
			};
			window.localStorage.setItem('oryx_user_id', 'test-user');
			if (migrated) window.localStorage.setItem('oryx_migrated_v1', 'true');
			for (const [k, v] of Object.entries(storage)) window.localStorage.setItem(k, v);
		},
	});
	const { document, localStorage, sessionStorage } = dom.window;
	const of = (prefix) => calls.filter(c => c.url.startsWith(prefix));
	return {
		dom, document, localStorage, sessionStorage, calls, outboxWrites,
		decryptageCalls: () => of('/decryptage'),
		ackCalls: () => of('/api/runtime/assistant-deliveries/'),
		messages: (key, role) => [...document.querySelectorAll(`#msgs-${key} .msg${role ? '.' + role : ''}`)],
		outbox: () => JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]'),
		async send(question = 'Que fait LVMH ?', ticker = 'MC.PA', key = 'decrypter') {
			document.getElementById('ticker-' + key).value = ticker;
			document.getElementById('input-' + key).value = question;
			document.getElementById('send-' + key).click();
			await settle();
			await waitFor(() => !document.getElementById('send-' + key).disabled);
			await settle();
		},
	};
}

test('Décrypter envoie user_id, conversation_key stable et un client_turn_id par tour', async () => {
	let n = 0;
	const app = mount({ handlers: { decryptage: () => response(decryptageSuccess(`00000000-0000-4000-8000-00000000000${++n}`)) } });
	await app.send('Premier tour');
	await app.send('Deuxième tour');
	const [a, b] = app.decryptageCalls().map(c => c.body);
	const conversationKey = app.sessionStorage.getItem(SESSION_KEY);
	assert.match(conversationKey, UUID_RE);
	for (const body of [a, b]) {
		assert.strictEqual(body.user_id, 'test-user');
		assert.strictEqual(body.conversation_key, conversationKey);
		assert.match(body.client_turn_id, UUID_RE);
		assert.deepStrictEqual(Object.keys(body).sort(), ['client_turn_id', 'context', 'conversation_key', 'last_method_id', 'level', 'question', 'ticker', 'user_id']);
	}
	assert.notStrictEqual(a.client_turn_id, b.client_turn_id);
	assert.strictEqual(a.question, 'Premier tour');
	assert.strictEqual(b.last_method_id, 'construction_these');
	app.dom.window.close();
});

test('retry réseau : même client_turn_id, un seul message user et un seul message assistant', async () => {
	let attempt = 0;
	const app = mount({ handlers: { decryptage: () => (++attempt === 1 ? Promise.reject(new TypeError('Failed to fetch')) : response(decryptageSuccess('22222222-2222-4222-8222-222222222222'))) } });
	await app.send();
	const bodies = app.decryptageCalls().map(c => c.body);
	assert.strictEqual(bodies.length, 2);
	assert.deepStrictEqual(bodies[0], bodies[1]);
	assert.strictEqual(app.messages('decrypter', 'user').length, 1);
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 1);
	assert.strictEqual(app.messages('decrypter', 'system').length, 0);
	app.dom.window.close();
});

test('deux échecs réseau : erreur retryable, aucun rendu assistant ni ACK', async () => {
	const app = mount({ handlers: { decryptage: () => Promise.reject(new TypeError('Failed to fetch')) } });
	await app.send();
	assert.strictEqual(app.decryptageCalls().length, 2);
	assert.strictEqual(app.messages('decrypter', 'user').length, 1);
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 0);
	assert.match(app.messages('decrypter', 'system')[0].textContent, /Réessaie/);
	assert.strictEqual(app.ackCalls().length, 0);
	app.dom.window.close();
});

test('ACK envoyé APRÈS le rendu de la réponse, puis retiré de l\'outbox', async () => {
	const turn = '33333333-3333-4333-8333-333333333333';
	const renderedAtAck = [];
	const outboxAtAck = [];
	const app = mount({
		handlers: {
			decryptage: () => response(decryptageSuccess(turn)),
			ack: (call, document) => {
				renderedAtAck.push(document.querySelectorAll('#msgs-decrypter .msg.assistant').length);
				outboxAtAck.push(JSON.parse(document.defaultView.localStorage.getItem(OUTBOX_KEY) || '[]').length);
				return response({ success: true, assistant_turn_id: turn, delivery_status: 'delivered' });
			},
		},
	});
	await app.send();
	await waitFor(() => app.ackCalls().length === 1);
	const [ack] = app.ackCalls();
	assert.strictEqual(ack.url, `/api/runtime/assistant-deliveries/${turn}/ack`);
	assert.strictEqual(ack.method, 'POST');
	assert.deepStrictEqual(ack.body, { user_id: 'test-user', conversation_key: app.sessionStorage.getItem(SESSION_KEY) });
	assert.deepStrictEqual(renderedAtAck, [1], 'la réponse est rendue avant l\'ACK');
	assert.strictEqual(app.outboxWrites[0].assistantRendered, 1, 'l\'outbox n\'est écrite qu\'après le rendu');
	assert.deepStrictEqual(outboxAtAck, [1], 'l\'ACK est en outbox avant l\'envoi');
	assert.ok(app.calls.indexOf(ack) > app.calls.indexOf(app.decryptageCalls()[0]));
	await waitFor(() => app.outbox().length === 0);
	assert.deepStrictEqual(app.outbox(), []);
	assert.strictEqual(app.messages('decrypter', 'assistant')[0].textContent.includes('Analyse pédagogique'), true);
	app.dom.window.close();
});

test('réponse déjà delivered (retry serveur) : rendue une fois, aucun ACK', async () => {
	const app = mount({ handlers: { decryptage: () => response(decryptageSuccess('44444444-4444-4444-8444-444444444444', { delivery_status: 'delivered' })) } });
	await app.send();
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 1);
	assert.strictEqual(app.ackCalls().length, 0);
	assert.deepStrictEqual(app.outbox(), []);
	app.dom.window.close();
});

test('ACK en échec : l\'entrée reste en outbox, sans aucun texte', async () => {
	const turn = '55555555-5555-4555-8555-555555555555';
	const app = mount({ handlers: { decryptage: () => response(decryptageSuccess(turn)), ack: () => response({ detail: 'x' }, { ok: false, status: 500 }) } });
	await app.send();
	await waitFor(() => app.ackCalls().length >= 1);
	await settle();
	assert.deepStrictEqual(app.outbox(), [{
		assistant_turn_id: turn, user_id: 'test-user', conversation_key: app.sessionStorage.getItem(SESSION_KEY), surface: 'decryptage',
	}]);
	const raw = app.localStorage.getItem(OUTBOX_KEY);
	for (const forbidden of ['Analyse', 'LVMH', 'Que fait', '600', 'disclaimer']) {
		assert.ok(!raw.includes(forbidden), forbidden);
	}
	app.dom.window.close();
});

test('rechargement : l\'ACK en attente est rejoué et retiré après succès', async () => {
	const entry = { assistant_turn_id: '66666666-6666-4666-8666-666666666666', user_id: 'test-user', conversation_key: 'conv-old', surface: 'decryptage' };
	const app = mount({ storage: { [OUTBOX_KEY]: JSON.stringify([entry]) } });
	await waitFor(() => app.outbox().length === 0);
	assert.deepStrictEqual(app.outbox(), []);
	const [ack] = app.ackCalls();
	assert.strictEqual(ack.url, `/api/runtime/assistant-deliveries/${entry.assistant_turn_id}/ack`);
	assert.deepStrictEqual(ack.body, { user_id: 'test-user', conversation_key: 'conv-old' });
	// L'ACK de chargement attend que l'utilisateur soit résolu.
	const userCall = app.calls.findIndex(c => c.url === '/api/user/test-user' && c.method === 'GET');
	assert.ok(userCall >= 0 && userCall < app.calls.indexOf(ack));
	app.dom.window.close();
});

test('flush ultérieur : un ACK d\'abord en échec réussit au flush suivant, avant le nouvel envoi', async () => {
	let online = false;
	let decryptageCount = 0;
	const successfulAcks = [];
	const app = mount({
		handlers: {
			decryptage: () => response(decryptageSuccess(`77777777-7777-4777-8777-77777777777${++decryptageCount}`)),
			ack: (call) => {
				if (!online) return Promise.reject(new TypeError('offline'));
				successfulAcks.push({ url: call.url, decryptageCallsSoFar: decryptageCount });
				return ackOk(call);
			},
		},
	});
	await app.send('Tour 1');
	await settle();
	assert.strictEqual(app.outbox().length, 1);
	online = true;
	await app.send('Tour 2');
	await waitFor(() => app.outbox().length === 0);
	assert.strictEqual(app.decryptageCalls().length, 2);
	assert.deepStrictEqual(successfulAcks, [
		{ url: '/api/runtime/assistant-deliveries/77777777-7777-4777-8777-777777777771/ack', decryptageCallsSoFar: 1 },
		{ url: '/api/runtime/assistant-deliveries/77777777-7777-4777-8777-777777777772/ack', decryptageCallsSoFar: 2 },
	]);
	app.dom.window.close();
});

test('nouvel envoi bloqué tant qu\'un ACK précédent ne peut pas être synchronisé', async () => {
	const entry = { assistant_turn_id: '88888888-8888-4888-8888-888888888888', user_id: 'test-user', conversation_key: 'conv-x', surface: 'decryptage' };
	const app = mount({ storage: { [OUTBOX_KEY]: JSON.stringify([entry]) }, handlers: { ack: () => response({}, { ok: false, status: 503 }) } });
	await settle();
	await app.send('Nouvelle question');
	assert.strictEqual(app.decryptageCalls().length, 0);
	assert.strictEqual(app.messages('decrypter', 'user').length, 0);
	assert.match(app.messages('decrypter', 'system')[0].textContent, /réessaie/i);
	assert.strictEqual(app.document.getElementById('input-decrypter').value, 'Nouvelle question', 'la saisie est conservée');
	assert.deepStrictEqual(app.outbox(), [entry]);
	app.dom.window.close();
});

test('nouvelle conversation bloquée si l\'ACK en attente échoue : rien n\'est réinitialisé', async () => {
	const app = mount({ handlers: { ack: () => Promise.reject(new TypeError('offline')) } });
	await app.send();
	const conversationKey = app.sessionStorage.getItem(SESSION_KEY);
	assert.strictEqual(app.outbox().length, 1);
	app.document.getElementById('new-convo-decrypter').click();
	await settle();
	await waitFor(() => app.messages('decrypter', 'system').length === 1);
	assert.strictEqual(app.sessionStorage.getItem(SESSION_KEY), conversationKey);
	assert.strictEqual(app.messages('decrypter', 'user').length, 1);
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 1);
	assert.match(app.messages('decrypter', 'system')[0].textContent, /conservée/);
	// L'historique de contexte n'est pas effacé : le tour suivant le porte.
	app.dom.window.close();
});

test('nouvelle conversation après flush réussi : nouveau conversation_key, même user_id, UI vidée', async () => {
	let n = 0;
	const app = mount({ handlers: { decryptage: () => response(decryptageSuccess(`99999999-9999-4999-8999-99999999999${++n}`)) } });
	await app.send('Tour 1');
	const before = app.sessionStorage.getItem(SESSION_KEY);
	await waitFor(() => app.outbox().length === 0);
	app.document.getElementById('new-convo-decrypter').click();
	await waitFor(() => app.sessionStorage.getItem(SESSION_KEY) !== before);
	const after = app.sessionStorage.getItem(SESSION_KEY);
	assert.match(after, UUID_RE);
	assert.notStrictEqual(after, before);
	assert.strictEqual(app.localStorage.getItem('oryx_user_id'), 'test-user');
	assert.strictEqual(app.messages('decrypter').length, 0);
	await app.send('Tour 2');
	const second = app.decryptageCalls()[1].body;
	assert.strictEqual(second.conversation_key, after);
	assert.strictEqual(second.user_id, 'test-user');
	assert.strictEqual(second.context, '', 'historique de contexte vidé');
	app.dom.window.close();
});

test('« Reprendre » : reset attendu puis envoi dans une nouvelle conversation', async () => {
	const app = mount({ handlers: { theses: () => response([{ ticker: 'MC.PA', current_step: 'moat', updated_at: '2026-10-04T10:00:00+00:00', theses: [] }]) } });
	await app.send('Tour 1');
	await waitFor(() => app.outbox().length === 0);
	const before = app.sessionStorage.getItem(SESSION_KEY);
	const resume = await waitFor(() => app.document.querySelector('.thesis-resume'));
	assert.ok(resume);
	resume.click();
	await waitFor(() => app.decryptageCalls().length === 2);
	await settle();
	const body = app.decryptageCalls()[1].body;
	assert.notStrictEqual(body.conversation_key, before);
	assert.strictEqual(body.conversation_key, app.sessionStorage.getItem(SESSION_KEY));
	assert.strictEqual(body.ticker, 'MC.PA');
	assert.strictEqual(body.context, '');
	assert.strictEqual(app.messages('decrypter', 'user').length, 1, 'ancienne conversation vidée avant l\'envoi');
	app.dom.window.close();
});

test('« Reprendre » bloqué si l\'ACK en attente échoue : aucun envoi', async () => {
	const entry = { assistant_turn_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', user_id: 'test-user', conversation_key: 'conv-y', surface: 'decryptage' };
	const app = mount({
		storage: { [OUTBOX_KEY]: JSON.stringify([entry]) },
		handlers: {
			ack: () => Promise.reject(new TypeError('offline')),
			theses: () => response([{ ticker: 'MC.PA', current_step: 'moat', updated_at: '2026-10-04T10:00:00+00:00', theses: [] }]),
		},
	});
	const before = app.sessionStorage.getItem(SESSION_KEY);
	const resume = await waitFor(() => app.document.querySelector('.thesis-resume'));
	resume.click();
	await settle();
	await settle();
	assert.strictEqual(app.decryptageCalls().length, 0);
	assert.strictEqual(app.sessionStorage.getItem(SESSION_KEY), before);
	app.dom.window.close();
});

test('Checklist inchangée : payload historique, aucun ACK ni identité runtime, pas de flush', async () => {
	const entry = { assistant_turn_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', user_id: 'test-user', conversation_key: 'conv-z', surface: 'decryptage' };
	const app = mount({ storage: { [OUTBOX_KEY]: JSON.stringify([entry]) }, handlers: { ack: () => Promise.reject(new TypeError('offline')) } });
	await settle();
	const acksBefore = app.ackCalls().length;
	await app.send('Checklist ?', 'AAPL', 'checklist');
	const calls = app.calls.filter(c => c.url === '/checklist');
	assert.strictEqual(calls.length, 1, 'un seul fetch, même si un ACK Décrypter est en attente');
	assert.deepStrictEqual(calls[0].body, { ticker: 'AAPL', question: 'Checklist ?', context: '', level: 'debutant', user_id: 'test-user' });
	assert.strictEqual(app.ackCalls().length, acksBefore, 'aucun ACK déclenché par Checklist');
	assert.strictEqual(app.messages('checklist', 'assistant').length, 1);
	assert.strictEqual(app.localStorage.getItem('oryx_v2_session_checklist'), null);
	assert.deepStrictEqual(app.outbox(), [entry]);
	app.dom.window.close();
});

test('Checklist : une erreur réseau n\'est pas rejouée (comportement réseau inchangé)', async () => {
	const app = mount({ handlers: { checklist: () => Promise.reject(new TypeError('Failed to fetch')) } });
	await app.send('Checklist ?', 'AAPL', 'checklist');
	assert.strictEqual(app.calls.filter(c => c.url === '/checklist').length, 1);
	assert.match(app.messages('checklist', 'system')[0].textContent, /Erreur réseau/);
	app.dom.window.close();
});

test('userReady : /decryptage attend le bootstrap utilisateur existant', async () => {
	let resolveUser;
	const pendingUser = new Promise(r => { resolveUser = r; });
	const app = mount({ handlers: { user: () => pendingUser } });
	app.document.getElementById('ticker-decrypter').value = 'MC.PA';
	app.document.getElementById('input-decrypter').value = 'Question';
	app.document.getElementById('send-decrypter').click();
	await settle();
	assert.strictEqual(app.decryptageCalls().length, 0, 'aucun /decryptage avant userReady');
	resolveUser({ ok: true, status: 200, json: () => Promise.resolve({ level: 'debutant', portfolio: [] }) });
	await waitFor(() => app.decryptageCalls().length === 1);
	assert.strictEqual(app.decryptageCalls().length, 1);
	const firstDecryptage = app.calls.indexOf(app.decryptageCalls()[0]);
	const userGets = app.calls.map((c, i) => [c, i]).filter(([c]) => c.url === '/api/user/test-user' && c.method === 'GET');
	assert.ok(userGets.length >= 1 && userGets.every(([, i]) => i < firstDecryptage));
	app.dom.window.close();
});

test('userReady en échec : aucun /decryptage, erreur retryable, puis nouvel essai réussi', async () => {
	let userOk = false;
	const app = mount({ handlers: { user: () => (userOk ? response({ level: 'debutant', portfolio: [] }) : response({}, { ok: false, status: 500 })) } });
	await app.send('Question');
	assert.strictEqual(app.decryptageCalls().length, 0);
	assert.match(app.messages('decrypter', 'system')[0].textContent, /Réessaie/);
	assert.strictEqual(app.messages('decrypter', 'user').length, 0);
	userOk = true;
	await app.send('Question');
	assert.strictEqual(app.decryptageCalls().length, 1);
	app.dom.window.close();
});

test('erreur HTTP de /decryptage (500) : message retryable, aucun ACK', async () => {
	const app = mount({ handlers: { decryptage: () => response({ detail: { error: 'Erreur interne', retryable: true } }, { ok: false, status: 500 }) } });
	await app.send();
	assert.strictEqual(app.decryptageCalls().length, 1, 'une erreur HTTP n\'est pas rejouée automatiquement');
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 0);
	assert.match(app.messages('decrypter', 'system')[0].textContent, /réessaie/i);
	assert.strictEqual(app.ackCalls().length, 0);
	app.dom.window.close();
});

// R1-C4 — conversation stale : l'analyse a avancé dans une autre conversation.
const STALE_409 = () => response({ detail: { error: 'stale_conversation_context', retryable: false } }, { ok: false, status: 409 });

test('409 stale_conversation_context : message clair, aucun rejeu, aucun ACK, aucun rattachement', async () => {
	const app = mount({ handlers: { decryptage: STALE_409 } });
	await app.send('Pour le moat, je pense que la marque compte.');
	await settle();
	assert.strictEqual(app.decryptageCalls().length, 1, 'jamais rejoué automatiquement');
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 0);
	assert.strictEqual(app.ackCalls().length, 0);
	assert.deepStrictEqual(app.outbox(), []);
	const [message] = app.messages('decrypter', 'system');
	assert.match(message.textContent, /Cette analyse a avancé dans une autre conversation\. Reprends l'étape actuelle pour continuer/);
	// Message non technique : ni code d'erreur, ni identifiant interne.
	assert.doesNotMatch(message.textContent, /stale|conversation_context|409|event|uuid/i);
	assert.doesNotMatch(message.textContent, /déjà été traité différemment/);
	// Aucun nouvel envoi silencieux (nouveau client_turn_id) : l'utilisateur décide.
	await settle();
	assert.strictEqual(app.decryptageCalls().length, 1);
	app.dom.window.close();
});

test('409 stale puis nouvel envoi décidé par l\'utilisateur : nouveau client_turn_id, même conversation', async () => {
	let calls = 0;
	const app = mount({ handlers: { decryptage: () => (++calls === 1 ? STALE_409() : response(decryptageSuccess('22222222-2222-4222-8222-222222222222'))) } });
	await app.send('Réponse moat.');
	await app.send('Et maintenant ?');
	const [first, second] = app.decryptageCalls();
	assert.notStrictEqual(first.body.client_turn_id, second.body.client_turn_id);
	assert.strictEqual(first.body.conversation_key, second.body.conversation_key);
	assert.strictEqual(second.body.question, 'Et maintenant ?');
	app.dom.window.close();
});

test('autre 409 (collision d\'identité) : message historique inchangé', async () => {
	const app = mount({ handlers: { decryptage: () => response({ detail: { error: 'AssistantTurnIdentityCollision', retryable: false } }, { ok: false, status: 409 }) } });
	await app.send();
	assert.match(app.messages('decrypter', 'system')[0].textContent, /déjà été traité différemment/);
	app.dom.window.close();
});

test('premier chargement (migration) : le portefeuille migré n\'est pas écrasé par un bootstrap anticipé', async () => {
	// Sans oryx_migrated_v1, initOryxUserSync migre d'abord (PUT niveau, POST
	// positions) puis lit /api/user ; userReady ne doit pas avancer ce GET.
	const app = mount({
		migrated: false,
		storage: { oryx_v2_portfolio: JSON.stringify([{ id: 1, ticker: 'MSFT', quantite: 2 }]) },
		handlers: { user: () => response({ level: 'debutant', portfolio: [{ id: 7, ticker: 'MSFT', quantite: 2 }] }) },
	});
	await waitFor(() => app.localStorage.getItem('oryx_migrated_v1') === 'true');
	await settle();
	const posts = app.calls.filter(c => c.url === '/api/user/test-user/portfolio' && c.method === 'POST');
	const firstGet = app.calls.findIndex(c => c.url === '/api/user/test-user' && c.method === 'GET');
	assert.ok(firstGet >= 0);
	assert.strictEqual(posts.length, 1);
	assert.ok(posts.every(p => app.calls.indexOf(p) < firstGet), 'migration avant lecture du profil');
	assert.strictEqual(app.calls.filter(c => c.url === '/api/user/test-user' && c.method === 'GET').length, 1);
	app.dom.window.close();
});


// --------------------------------------------------------------------------
// Fail-closed : seul un ACK 2xx valide retire une entrée de l'outbox.
// --------------------------------------------------------------------------

const THESES = [{ ticker: 'MC.PA', current_step: 'moat', updated_at: '2026-10-04T10:00:00+00:00', theses: [] }];

// Un tour Décrypter rendu dont l'ACK obtient `ackResponse` ; retourne l'app
// avec l'entrée d'outbox correspondante.
async function renderedTurnWithAck(ackResponse, { turn = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', handlers = {} } = {}) {
	const app = mount({ handlers: { decryptage: () => response(decryptageSuccess(turn)), ack: ackResponse, ...handlers } });
	await app.send('Tour rendu');
	await waitFor(() => app.ackCalls().length >= 1);
	await settle();
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 1);
	assert.deepStrictEqual(app.outbox(), [{
		assistant_turn_id: turn, user_id: 'test-user', conversation_key: app.sessionStorage.getItem(SESSION_KEY), surface: 'decryptage',
	}], 'entrée conservée');
	return app;
}

async function assertSendBlocked(app) {
	const acksBefore = app.ackCalls().length;
	await app.send('Nouvelle question');
	assert.strictEqual(app.decryptageCalls().length, 1, 'aucun nouvel appel /decryptage');
	assert.ok(app.ackCalls().length > acksBefore, 'le flush a bien été retenté');
	assert.match(app.messages('decrypter', 'system').at(-1).textContent, /Synchronisation de la réponse précédente impossible\. Recharge la page ou réessaie\./);
	assert.strictEqual(app.document.getElementById('input-decrypter').value, 'Nouvelle question');
	assert.strictEqual(app.outbox().length, 1);
}

for (const [label, ack] of [
	['ACK 500', () => response({ detail: { error: 'x', retryable: true } }, { ok: false, status: 500 })],
	['ACK 403', () => response({ detail: { error: 'AssistantDeliveryOwnershipConflict', retryable: false } }, { ok: false, status: 403 })],
	['ACK 409', () => response({ detail: { error: 'x', retryable: false } }, { ok: false, status: 409 })],
	['ACK réseau', () => Promise.reject(new TypeError('Failed to fetch'))],
	['ACK 2xx invalide (statut pending)', (call) => response({ success: true, assistant_turn_id: decodeURIComponent(call.url.split('/')[4]), delivery_status: 'pending' })],
	['ACK 2xx invalide (autre assistant_turn_id)', () => response({ success: true, assistant_turn_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', delivery_status: 'delivered' })],
	['ACK 2xx invalide (corps non JSON)', () => Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new SyntaxError('bad json')) })],
]) {
	test(`${label} : entrée conservée, nouvel envoi bloqué`, async () => {
		const app = await renderedTurnWithAck(ack);
		await assertSendBlocked(app);
		app.dom.window.close();
	});
}

test('ACK 404 : entrée conservée, nouvelle conversation bloquée', async () => {
	const app = await renderedTurnWithAck(() => response({ detail: { error: 'AssistantDeliveryNotFound', retryable: false } }, { ok: false, status: 404 }));
	const conversationKey = app.sessionStorage.getItem(SESSION_KEY);
	app.document.getElementById('new-convo-decrypter').click();
	await waitFor(() => app.messages('decrypter', 'system').length === 1);
	assert.match(app.messages('decrypter', 'system')[0].textContent, /conversation est conservée\. Recharge la page ou réessaie\./);
	assert.strictEqual(app.sessionStorage.getItem(SESSION_KEY), conversationKey);
	assert.strictEqual(app.messages('decrypter', 'user').length, 1);
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 1);
	assert.strictEqual(app.outbox().length, 1);
	app.dom.window.close();
});

test('ACK 422 : entrée conservée, « Reprendre » bloqué', async () => {
	const app = await renderedTurnWithAck(() => response({ detail: [] }, { ok: false, status: 422 }), { handlers: { theses: () => response(THESES) } });
	const conversationKey = app.sessionStorage.getItem(SESSION_KEY);
	const resume = await waitFor(() => app.document.querySelector('.thesis-resume'));
	assert.ok(resume);
	resume.click();
	await settle();
	await settle();
	assert.strictEqual(app.decryptageCalls().length, 1, 'aucun envoi « Reprendre »');
	assert.strictEqual(app.sessionStorage.getItem(SESSION_KEY), conversationKey);
	assert.strictEqual(app.messages('decrypter', 'assistant').length, 1);
	assert.strictEqual(app.outbox().length, 1);
	app.dom.window.close();
});

test('ACK 2xx valide : entrée retirée, envoi puis nouvelle conversation autorisés', async () => {
	let n = 0;
	const app = mount({ handlers: { decryptage: () => response(decryptageSuccess(`eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee${++n}`)) } });
	await app.send('Tour 1');
	await waitFor(() => app.outbox().length === 0);
	assert.deepStrictEqual(app.outbox(), []);
	await app.send('Tour 2');
	assert.strictEqual(app.decryptageCalls().length, 2);
	await waitFor(() => app.outbox().length === 0);
	const before = app.sessionStorage.getItem(SESSION_KEY);
	app.document.getElementById('new-convo-decrypter').click();
	await waitFor(() => app.sessionStorage.getItem(SESSION_KEY) !== before);
	assert.notStrictEqual(app.sessionStorage.getItem(SESSION_KEY), before);
	assert.strictEqual(app.messages('decrypter').length, 0);
	assert.strictEqual(app.messages('decrypter', 'system').length, 0);
	app.dom.window.close();
});

test('outbox : jamais de texte de réponse, de question ni de donnée financière, quel que soit l\'échec', async () => {
	const app = await renderedTurnWithAck(() => response({}, { ok: false, status: 403 }));
	await assertSendBlocked(app);
	const raw = app.localStorage.getItem(OUTBOX_KEY);
	for (const forbidden of ['Analyse', 'pédagogique', 'LVMH', 'Tour rendu', 'Nouvelle question', '600', 'EUR', 'disclaimer', 'analysis']) {
		assert.ok(!raw.includes(forbidden), forbidden);
	}
	assert.deepStrictEqual(Object.keys(JSON.parse(raw)[0]).sort(), ['assistant_turn_id', 'conversation_key', 'surface', 'user_id']);
	app.dom.window.close();
});
