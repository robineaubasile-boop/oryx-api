// Vérifie l'édition d'une position du portefeuille : le bouton ✎ affiche un
// formulaire pré-rempli, et « Enregistrer » envoie un PUT puis ré-affiche la
// carte avec les nouvelles valeurs.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const HTML = fs.readFileSync(path.join(__dirname, '../../web-v2/index.html'), 'utf8');

function jsonResponse(body) {
	return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
}

async function waitFor(fn) {
	for (let i = 0; i < 100; i++) {
		const result = fn();
		if (result) return result;
		await new Promise(r => setTimeout(r, 10));
	}
	return null;
}

test('édition d\'une position : formulaire pré-rempli puis PUT et ré-affichage', async () => {
	const calls = [];
	function mockFetch(url, options = {}) {
		calls.push({ url, method: options.method || 'GET', body: options.body });
		if (url.startsWith('/web-v2/search')) {
			return jsonResponse({ success: true, name: 'Microsoft', currency: 'USD', price: 352 });
		}
		if (url.startsWith('/web-v2/fx-rate')) {
			return jsonResponse({ success: true, eur_usd: 1.17 });
		}
		if (options.method === 'PUT') {
			const body = JSON.parse(options.body);
			return jsonResponse({ id: 1, ticker: body.ticker, quantite: body.quantity, prixAchat: body.purchase_price, cible: body.target_percent, enveloppe: body.envelope });
		}
		return jsonResponse({});
	}

	const dom = new JSDOM(HTML, {
		url: 'http://localhost/web-v2/',
		runScripts: 'dangerously',
		pretendToBeVisual: true,
		beforeParse(window) {
			window.fetch = mockFetch;
			window.localStorage.setItem('oryx_user_id', 'test-user');
			window.localStorage.setItem('oryx_migrated_v1', 'true');
			window.localStorage.setItem('oryx_v2_portfolio', JSON.stringify([
				{ id: 1, ticker: 'MSFT', quantite: 10, prixAchat: 232, cible: 25, enveloppe: 'CTO' },
			]));
		},
	});
	const { document } = dom.window;

	const editBtn = await waitFor(() => document.querySelector('.position-card-edit'));
	assert.ok(editBtn, 'le bouton d\'édition doit être rendu');
	editBtn.click();

	const quantiteInput = await waitFor(() => document.getElementById('pf-edit-quantite-1'));
	assert.ok(quantiteInput, 'le formulaire d\'édition doit apparaître');
	assert.strictEqual(quantiteInput.value, '10');
	assert.strictEqual(document.getElementById('pf-edit-prix-achat-1').value, '232');
	assert.strictEqual(document.getElementById('pf-edit-cible-1').value, '25');
	assert.strictEqual(document.getElementById('pf-edit-enveloppe-1').value, 'CTO');
	assert.ok(!document.querySelector('.position-card-delete'), 'la carte normale est remplacée par le formulaire');

	quantiteInput.value = '15';
	document.querySelector('.position-edit-save').click();

	const quantiteValue = await waitFor(() => {
		if (document.getElementById('pf-edit-quantite-1')) return null;
		const item = [...document.querySelectorAll('.position-stat-item')]
			.find(el => el.querySelector('.position-stat-label').textContent === 'Quantité');
		return item ? item.querySelector('.position-stat-value').textContent : null;
	});
	assert.strictEqual(quantiteValue, '15.0000');

	const putCall = calls.find(c => c.method === 'PUT' && c.url.includes('/portfolio/'));
	assert.ok(putCall, 'fetch doit être appelé en PUT');
	assert.strictEqual(putCall.url, '/api/user/test-user/portfolio/1');
	assert.deepStrictEqual(JSON.parse(putCall.body), {
		ticker: 'MSFT', quantity: 15, purchase_price: 232, target_percent: 25, envelope: 'CTO',
	});

	const stored = JSON.parse(dom.window.localStorage.getItem('oryx_v2_portfolio'));
	assert.strictEqual(stored[0].quantite, 15);

	dom.window.close();
});
