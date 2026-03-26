/**
 * Databricks Secret Sync
 * GET  /api/databricks-sync - Get latest sync workflow status
 * POST /api/databricks-sync - Trigger sync workflow
 *
 * Validates that Databricks credentials exist in KV, then triggers the
 * GitHub Actions databricks-sync.yml workflow. The workflow reads
 * credentials directly from KV via Cloudflare API (no secrets in inputs).
 */

import { logApiCall, logError } from './_utils/logger.js';

export async function onRequestGet(context) {
  const { env } = context;

  if (!env.GITHUB_TOKEN || !env.GITHUB_OWNER || !env.GITHUB_REPO) {
    return new Response(JSON.stringify({ success: false, error: 'Missing env vars' }), {
      status: 500, headers: { 'Content-Type': 'application/json' },
    });
  }

  try {
    const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/databricks-sync.yml/runs?per_page=1`;
    const res = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'Nexus-Stack-Control-Plane',
      },
    });

    if (!res.ok) {
      return new Response(JSON.stringify({ success: true, status: 'unknown', message: 'No sync runs found' }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }

    const data = await res.json();
    const run = data.workflow_runs?.[0];

    if (!run) {
      return new Response(JSON.stringify({ success: true, status: 'never', message: 'No sync has been run yet' }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }

    return new Response(JSON.stringify({
      success: true,
      status: run.status === 'completed' ? run.conclusion : run.status,
      updated_at: run.updated_at,
      message: run.status === 'completed'
        ? `Last sync: ${run.conclusion} (${new Date(run.updated_at).toLocaleString()})`
        : `Sync ${run.status}...`,
    }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    return new Response(JSON.stringify({ success: true, status: 'unknown', message: 'Could not fetch status' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }
}

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
    const hasToken = !!(await env.NEXUS_KV.get('databricks_token'));

    if (!host || !hasToken) {
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

    // Trigger workflow without secrets — workflow reads from KV via Cloudflare API
    const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/databricks-sync.yml/dispatches`;

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'Nexus-Stack-Control-Plane',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: 'main' }),
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
