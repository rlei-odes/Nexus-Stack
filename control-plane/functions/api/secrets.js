/**
 * GET /api/secrets — Fetches secrets live from Infisical, grouped by folder.
 *
 * Thin wrapper around fetchAllInfisicalSecrets() — the fetch logic is shared
 * with POST /api/databricks-sync so the Secrets page and the Databricks scope
 * always mirror the same inventory.
 *
 * All Control Panel endpoints are protected by Cloudflare Access (email OTP)
 * at the infrastructure level. No additional auth needed.
 */
import { fetchAllInfisicalSecrets } from './_utils/infisical.js';

export async function onRequestGet(context) {
  try {
    const { configured, groups, warnings, message } = await fetchAllInfisicalSecrets(context.env);

    if (!configured) {
      return Response.json({ success: true, groups: [], message });
    }

    const response = { success: true, groups };
    if (warnings && warnings.length > 0) response.warnings = warnings;
    return Response.json(response);
  } catch (error) {
    return Response.json({ success: false, error: error.message }, { status: 500 });
  }
}
