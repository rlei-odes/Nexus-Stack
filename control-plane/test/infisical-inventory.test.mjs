// The Secrets page listed folders only up to roughly the fiftieth: a Pages
// Function may make 50 subrequests per invocation on the free plan, and the
// inventory made one per Infisical folder, of which a stack has over a hundred.
// The rest failed into a `warnings` array the page never rendered.

import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';

import { fetchAllInfisicalSecrets, groupSecretsByFolder } from '../functions/api/_utils/infisical.js';

const ENV = { INFISICAL_TOKEN: 'tok', INFISICAL_PROJECT_ID: 'proj', DOMAIN: 'example.com' };

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

function captureFetch(respond) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return respond(url, init);
  };
  return calls;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** One secret per folder, `count` folders, named so they sort like real stacks. */
function manyFolders(count) {
  return Array.from({ length: count }, (_, i) => ({
    secretKey: 'PASSWORD',
    secretValue: `pw-${i}`,
    secretPath: `/stack-${String(i).padStart(3, '0')}`,
  }));
}

test('the whole inventory costs one subrequest, however many folders there are', async () => {
  const calls = captureFetch(() => json({ secrets: manyFolders(120) }));

  const { configured, groups, warnings } = await fetchAllInfisicalSecrets(ENV);

  assert.equal(calls.length, 1);
  assert.equal(configured, true);
  assert.equal(groups.length, 120);
  assert.equal(groups.at(-1).name, 'stack-119');
  assert.deepEqual(warnings, []);
});

test('the request is recursive from the root of the configured environment', async () => {
  const calls = captureFetch(() => json({ secrets: [] }));

  await fetchAllInfisicalSecrets({ ...ENV, INFISICAL_ENV: 'prod' });

  const url = new URL(calls[0].url);
  assert.equal(url.origin + url.pathname, 'https://infisical.example.com/api/v3/secrets/raw');
  assert.equal(url.searchParams.get('workspaceId'), 'proj');
  assert.equal(url.searchParams.get('environment'), 'prod');
  assert.equal(url.searchParams.get('secretPath'), '/');
  assert.equal(url.searchParams.get('recursive'), 'true');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer tok');
});

test('a failed request throws instead of returning part of the inventory', async () => {
  captureFetch(() => new Response('upstream down', { status: 502 }));

  await assert.rejects(fetchAllInfisicalSecrets(ENV), /Failed to fetch secrets from Infisical \(502\)/);
});

test('without Infisical configuration nothing is fetched', async () => {
  const calls = captureFetch(() => json({ secrets: [] }));

  const result = await fetchAllInfisicalSecrets({ DOMAIN: 'example.com' });

  assert.equal(result.configured, false);
  assert.equal(calls.length, 0);
});

test('secrets are grouped by folder, root and nested paths included', () => {
  const groups = groupSecretsByFolder([
    { secretKey: 'PORTAINER_USERNAME', secretValue: 'nexus', secretPath: '/portainer' },
    { secretKey: 'PORTAINER_PASSWORD', secretValue: 'pw', secretPath: '/portainer' },
    { secretKey: 'ADMIN_EMAIL', secretValue: 'a@example.com', secretPath: '/' },
    { secretKey: 'NO_PATH', secretValue: 'x' },
    { secretKey: 'DEEP', secretValue: 'y', secretPath: '/parent/child/' },
  ]);

  assert.deepEqual(groups, [
    { name: 'parent/child', secrets: [{ key: 'DEEP', value: 'y' }] },
    { name: 'portainer', secrets: [
      { key: 'PORTAINER_PASSWORD', value: 'pw' },
      { key: 'PORTAINER_USERNAME', value: 'nexus' },
    ] },
    { name: 'root', secrets: [{ key: 'ADMIN_EMAIL', value: 'a@example.com' }, { key: 'NO_PATH', value: 'x' }] },
  ]);
});

test('empty values are dropped, and a folder left empty is not listed', () => {
  const groups = groupSecretsByFolder([
    { secretKey: 'EMPTY', secretValue: '', secretPath: '/a' },
    { secretKey: 'MISSING', secretPath: '/a' },
    { secretKey: 'KEPT', secretValue: 'v', secretPath: '/b' },
  ]);

  assert.deepEqual(groups, [{ name: 'b', secrets: [{ key: 'KEPT', value: 'v' }] }]);
});
