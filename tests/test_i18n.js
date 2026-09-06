const test = require('node:test');
const assert = require('node:assert/strict');
const {normalizeLocale, detectLocale} = require('../src/gui/static/i18n.js');

test('normalizes Portuguese and Spanish Windows locales', () => {
  assert.equal(normalizeLocale('pt-PT'), 'pt-BR');
  assert.equal(normalizeLocale('pt-BR'), 'pt-BR');
  assert.equal(normalizeLocale('es-MX'), 'es');
  assert.equal(normalizeLocale('fr-FR'), 'en');
});

test('saved preference wins over browser languages', () => {
  assert.equal(detectLocale('es', ['pt-BR', 'en-US']), 'es');
});

test('browser language is used when there is no saved preference', () => {
  assert.equal(detectLocale(null, ['pt-BR', 'en-US']), 'pt-BR');
  assert.equal(detectLocale(null, ['es-AR', 'en-US']), 'es');
  assert.equal(detectLocale(null, ['de-DE']), 'en');
});
