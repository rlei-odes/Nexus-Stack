/**
 * Trigger Databricks Secret Sync
 * POST /api/databricks-sync
 *
 * Reads Databricks credentials from KV, triggers the GitHub Actions
 * databricks-sync.yml workflow which reads secrets from Infisical
 * and pushes them to Databricks Secret Scopes.
 */

import { logApiCall, logError } from './_utils/logger.js';

export async function onRequestPost(context) {
  const { env } = context;

  if (!env.GITHUB_TOKEN || !env.GITHUB_OWNER || !env.GITHUB_REPO) {
    return new Response(JSON.stringify({
      success: false,
      error: 'Missing required environment variables',
    }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  try {
    if (!env.NEXUS_KV) {
      return new Response(JSON.stringify({ success: false, error: 'KV not configured' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const host = await env.NEXUS_KV.get('databricks_host');
    const token = await env.NEXUS_KV.get('databricks_token');

    if (!host || !token) {
      return new Response(JSON.stringify({
        success: false,
        error: 'Databricks not configured. Save host and token first.',
      }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    await logApiCall(env.NEXUS_DB, '/api/databricks-sync', 'POST', {
      action: 'trigger_databricks_sync',
      host,
    });

    // Trigger the sync workflow with credentials as inputs
    const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/databricks-sync.yml/dispatches`;

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'Nexus-Stack-Control-Plane',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        ref: 'main',
        inputs: {
          databricks_host: host,
          databricks_token: token,
        },
      }),
    });

    if (response.status === 204) {
      return new Response(JSON.stringify({
        success: true,
        message: 'Databricks sync workflow triggered',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const errorText = await response.text();
    await logError(env.NEXUS_DB, '/api/databricks-sync', `GitHub API error: ${response.status}`, new Error(errorText));

    return new Response(JSON.stringify({
      success: false,
      error: `GitHub API returned ${response.status}`,
    }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    await logError(env.NEXUS_DB, '/api/databricks-sync', 'Failed to trigger sync', error);
    return new Response(JSON.stringify({ success: false, error: 'Failed to trigger sync' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
