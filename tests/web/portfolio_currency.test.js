// Vérifie que renderPortfolio() calcule la performance d'une position USD
// en euros : le prix d'achat est saisi en EUR, le cours actuel arrive en USD
// et doit être converti via le taux EUR/USD avant la comparaison.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const HTML = fs.readFileSync(path.join(__dirname, '../../web-v2/index.html'), 'utf8');

function jsonResponse(body) {
	return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
}

function mockFetch(url) {
	if (url.startsWith('/web-v2/search')) {
		return jsonResponse({ success: true, name: 'Microsoft', currency: 'USD', price: 352 });
	}
	if (url.startsWith('/web-v2/fx-rate')) {
		return jsonResponse({ success: true, eur_usd: 1.17 });
	}
	return jsonResponse({});
}

test('position USD : gainPct et plus-value calculés en euros', async () => {
	const dom = new JSDOM(HTML, {
		url: 'http://localhost/web-v2/',
		runScripts: 'dangerously',
		pretendToBeVisual: true,
		beforeParse(window) {
			window.fetch = mockFetch;
			window.localStorage.setItem('oryx_user_id', 'test-user');
			window.localStorage.setItem('oryx_migrated_v1', 'true');
			window.localStorage.setItem('oryx_v2_portfolio', JSON.stringify([
				{ id: 1, ticker: 'MSFT', quantite: 10, prixAchat: 232, cible: null, enveloppe: 'CTO' },
			]));
		},
	});
	const { document } = dom.window;

	// Attend que renderPortfolio() ait remplacé l'état de chargement.
	for (let i = 0; i < 50 && !document.querySelector('.position-card'); i++) {
		await new Promise(r => setTimeout(r, 10));
	}
	const card = document.querySelector('.position-card');
	assert.ok(card, 'la carte de position doit être rendue');

	const stats = {};
	card.querySelectorAll('.position-stat-item').forEach(item => {
		stats[item.querySelector('.position-stat-label').textContent] = item.querySelector('.position-stat-value').textContent;
	});
	const summaryPct = card.querySelector('.position-card-summary-pct').textContent;

	// 352 USD / 1.17 = 300.85 € -> (300.85 - 232) / 232 = +29.7 %
	const prixActuelEur = 352 / 1.17;
	const expectedPct = ((prixActuelEur - 232) / 232) * 100;
	const expectedPctStr = '+' + expectedPct.toFixed(1) + '%';
	const oldBuggyPctStr = '+' + (((352 - 232) / 232) * 100).toFixed(1) + '%'; // +51.7 %

	assert.strictEqual(summaryPct, expectedPctStr);
	assert.strictEqual(stats['Plus-value %'], expectedPctStr);
	assert.notStrictEqual(summaryPct, oldBuggyPctStr);

	const valeurEur = 10 * 352 / 1.17;
	assert.strictEqual(stats['Valeur'], valeurEur.toFixed(2) + ' €');
	assert.strictEqual(stats['Plus-value'], '+' + (valeurEur - 10 * 232).toFixed(2));

	dom.window.close();
});
