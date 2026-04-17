/**
 * GET /api/secrets — Fetches secrets live from Infisical API, grouped by folder.
 *
 * Reads INFISICAL_TOKEN, INFISICAL_PROJECT_ID, and DOMAIN from environment
 * to connect to the Infisical instance running on the Nexus Stack server.
 * Secrets are organized by Infisical folders (one per service).
 *
 * Uses CF-Access-Client-Id/Secret headers to bypass Cloudflare Access
 * for server-to-server authentication (no browser login required).
 */
import { fetchWithTimeout } from './_utils/fetch-with-timeout.js';
import { safeHttpsUrl } from './_utils/url.js';

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

export async function onRequestGet(context) {
  try {
    // All Control Panel endpoints are protected by Cloudflare Access (email OTP)
    // at the infrastructure level (configured in Terraform). No additional auth needed.
    // Note: CF-Access-Authenticated-User-Email header is not reliably forwarded
    // to Cloudflare Pages Functions, so we cannot check it here.

    const token = context.env.INFISICAL_TOKEN;
    const projectId = context.env.INFISICAL_PROJECT_ID;
    const domain = context.env.DOMAIN;

    if (!token || !projectId || !domain) {
      return Response.json({
        success: true,
        groups: [],
        message: 'Infisical not configured. Ensure INFISICAL_TOKEN, INFISICAL_PROJECT_ID, and DOMAIN are set.',
      });
    }

    const baseUrl = safeHttpsUrl(context.env.INFISICAL_URL, `https://infisical.${domain}`);
    const environment = context.env.INFISICAL_ENV || 'dev';
    const headers = {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
    };

    // Add Cloudflare Access Service Token headers for machine-to-machine auth
    const cfAccessClientId = context.env.CF_ACCESS_CLIENT_ID;
    const cfAccessClientSecret = context.env.CF_ACCESS_CLIENT_SECRET;
    if (cfAccessClientId && cfAccessClientSecret) {
      headers['CF-Access-Client-Id'] = cfAccessClientId;
      headers['CF-Access-Client-Secret'] = cfAccessClientSecret;
    }

    // Step 1: List all folders
    const foldersRes = await fetchWithTimeout(
      `${baseUrl}/api/v1/folders?workspaceId=${projectId}&environment=${environment}&path=/`,
      { headers }
    );

    if (!foldersRes.ok) {
      const errText = await foldersRes.text();
      return Response.json({
        success: false,
        error: `Failed to fetch folders from Infisical (${foldersRes.status}): ${errText.substring(0, 200)}`,
      }, { status: 502 });
    }

    const foldersData = await safeJsonParse(foldersRes, 'Folders API');
    const folders = foldersData.folders || [];

    // Step 2: Fetch secrets from each folder in parallel
    const warnings = [];
    const folderPromises = folders.map(async (folder) => {
      try {
        const secretsRes = await fetchWithTimeout(
          `${baseUrl}/api/v3/secrets/raw?workspaceId=${projectId}&environment=${environment}&secretPath=/${folder.name}`,
          { headers }
        );

        if (!secretsRes.ok) {
          warnings.push(`${folder.name}: HTTP ${secretsRes.status}`);
          return null;
        }

        const secretsData = await safeJsonParse(secretsRes, `Secrets API (${folder.name})`);
        const secrets = (secretsData.secrets || [])
          .filter(s => s.secretValue !== undefined && s.secretValue !== '')
          .map(s => ({
            key: s.secretKey,
            value: s.secretValue,
          }))
          .sort((a, b) => a.key.localeCompare(b.key));

        if (secrets.length === 0) return null;

        return {
          name: folder.name,
          secrets,
        };
      } catch (err) {
        warnings.push(`${folder.name}: ${err.message}`);
        return null;
      }
    });

    // Also fetch root-level secrets (/)
    folderPromises.push(
      (async () => {
        try {
          const rootRes = await fetchWithTimeout(
            `${baseUrl}/api/v3/secrets/raw?workspaceId=${projectId}&environment=${environment}&secretPath=/`,
            { headers }
          );
          if (!rootRes.ok) {
            warnings.push(`root: HTTP ${rootRes.status}`);
            return null;
          }
          const rootData = await safeJsonParse(rootRes, 'Root secrets API');
          const secrets = (rootData.secrets || [])
            .filter(s => s.secretValue !== undefined && s.secretValue !== '')
            .map(s => ({ key: s.secretKey, value: s.secretValue }))
            .sort((a, b) => a.key.localeCompare(b.key));
          if (secrets.length === 0) return null;
          return { name: 'root', secrets };
        } catch (err) {
          warnings.push(`root: ${err.message}`);
          return null;
        }
      })()
    );

    const results = await Promise.all(folderPromises);
    const groups = results
      .filter(Boolean)
      .sort((a, b) => a.name.localeCompare(b.name));

    const response = { success: true, groups };
    if (warnings.length > 0) {
      response.warnings = warnings;
    }
    return Response.json(response);
  } catch (error) {
    return Response.json({ success: false, error: error.message }, { status: 500 });
  }
}
