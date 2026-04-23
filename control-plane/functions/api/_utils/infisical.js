/**
 * Shared Infisical fetch helper.
 *
 * Enumerates every folder under the project's root, fetches all key/value
 * pairs per folder (and at root path `/`), and returns a grouped+sorted
 * structure. Consumed by:
 *   - GET /api/secrets          (renders the Secrets page)
 *   - POST /api/databricks-sync (pushes the same inventory into Databricks)
 *
 * Keeping both endpoints on one code path guarantees the UI and the Databricks
 * scope stay in lockstep — whatever you see on /secrets is exactly what a
 * sync will upsert.
 */
import { fetchWithTimeout } from './fetch-with-timeout.js';
import { safeHttpsUrl } from './url.js';

async function safeJsonParse(response, label) {
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    const bodyPreview = (await response.text()).substring(0, 200);
    throw new Error(
      `${label} returned non-JSON response (${response.status}, content-type: ${contentType}). ` +
      `This usually means Cloudflare Access is blocking the request. Preview: ${bodyPreview}`
    );
  }
  return response.json();
}

/**
 * Fetch every secret from the Infisical project.
 *
 * @param {object} env - Pages Function env bindings
 * @returns {Promise<{
 *   configured: boolean,
 *   groups: Array<{ name: string, secrets: Array<{ key: string, value: string }> }>,
 *   warnings: string[],
 *   message?: string,
 * }>}
 *
 * When Infisical env vars are missing, returns `{ configured: false, groups: [], warnings: [], message }`
 * rather than throwing, matching the prior /api/secrets behaviour.
 *
 * Thrown errors from this helper are only for unexpected failures (e.g. the folders
 * endpoint itself 5xx'd) — the caller should catch and surface them.
 */
export async function fetchAllInfisicalSecrets(env) {
  const token = env.INFISICAL_TOKEN;
  const projectId = env.INFISICAL_PROJECT_ID;
  const domain = env.DOMAIN;

  if (!token || !projectId || !domain) {
    return {
      configured: false,
      groups: [],
      warnings: [],
      message: 'Infisical not configured. Ensure INFISICAL_TOKEN, INFISICAL_PROJECT_ID, and DOMAIN are set.',
    };
  }

  const baseUrl = safeHttpsUrl(env.INFISICAL_URL, `https://infisical.${domain}`);
  if (!baseUrl) {
    // safeHttpsUrl returns '' if both candidate and fallback fail to parse as
    // https URLs. Without this guard, fetches below would resolve as relative
    // paths against the Control Plane origin and leak the Infisical bearer
    // token to the wrong host.
    return {
      configured: false,
      groups: [],
      warnings: [],
      message: `Infisical misconfigured: INFISICAL_URL is invalid and fallback https://infisical.${domain} did not parse as https.`,
    };
  }
  const environment = env.INFISICAL_ENV || 'dev';
  const headers = {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json',
  };

  // Cloudflare Access service-token headers for machine-to-machine auth
  const cfAccessClientId = env.CF_ACCESS_CLIENT_ID;
  const cfAccessClientSecret = env.CF_ACCESS_CLIENT_SECRET;
  if (cfAccessClientId && cfAccessClientSecret) {
    headers['CF-Access-Client-Id'] = cfAccessClientId;
    headers['CF-Access-Client-Secret'] = cfAccessClientSecret;
  }

  // Step 1: list all folders under /
  const foldersRes = await fetchWithTimeout(
    `${baseUrl}/api/v1/folders?workspaceId=${projectId}&environment=${environment}&path=/`,
    { headers }
  );

  if (!foldersRes.ok) {
    const errText = await foldersRes.text();
    throw new Error(`Failed to fetch folders from Infisical (${foldersRes.status}): ${errText.substring(0, 200)}`);
  }

  const foldersData = await safeJsonParse(foldersRes, 'Folders API');
  const folders = foldersData.folders || [];

  // Step 2: fetch secrets from each folder (and the root path) in parallel
  const warnings = [];
  const fetchGroup = async (folderName, secretPath) => {
    try {
      const res = await fetchWithTimeout(
        `${baseUrl}/api/v3/secrets/raw?workspaceId=${projectId}&environment=${environment}&secretPath=${secretPath}`,
        { headers }
      );
      if (!res.ok) {
        warnings.push(`${folderName}: HTTP ${res.status}`);
        return null;
      }
      const data = await safeJsonParse(res, `Secrets API (${folderName})`);
      const secrets = (data.secrets || [])
        .filter(s => s.secretValue !== undefined && s.secretValue !== '')
        .map(s => ({ key: s.secretKey, value: s.secretValue }))
        .sort((a, b) => a.key.localeCompare(b.key));
      if (secrets.length === 0) return null;
      return { name: folderName, secrets };
    } catch (err) {
      warnings.push(`${folderName}: ${err.message}`);
      return null;
    }
  };

  const groupPromises = folders.map(f => fetchGroup(f.name, `/${f.name}`));
  groupPromises.push(fetchGroup('root', '/'));

  const results = await Promise.all(groupPromises);
  const groups = results
    .filter(Boolean)
    .sort((a, b) => a.name.localeCompare(b.name));

  return { configured: true, groups, warnings };
}
