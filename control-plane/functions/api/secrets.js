/**
 * GET /api/secrets — Fetches secrets live from Infisical API, grouped by folder.
 *
 * Reads INFISICAL_TOKEN, INFISICAL_PROJECT_ID, and DOMAIN from environment
 * to connect to the Infisical instance running on the Nexus Stack server.
 * Secrets are organized by Infisical folders (one per service).
 */

export async function onRequestGet(context) {
  try {
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

    const baseUrl = `https://infisical.${domain}`;
    const environment = 'dev';
    const headers = {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
    };

    // Step 1: List all folders
    const foldersRes = await fetch(
      `${baseUrl}/api/v1/folders?workspaceId=${projectId}&environment=${environment}&path=/`,
      { headers }
    );

    if (!foldersRes.ok) {
      const errText = await foldersRes.text();
      return Response.json({
        success: false,
        error: `Failed to fetch folders from Infisical (${foldersRes.status}): ${errText}`,
      }, { status: 502 });
    }

    const foldersData = await foldersRes.json();
    const folders = foldersData.folders || [];

    // Step 2: Fetch secrets from each folder in parallel
    const folderPromises = folders.map(async (folder) => {
      try {
        const secretsRes = await fetch(
          `${baseUrl}/api/v3/secrets/raw?workspaceId=${projectId}&environment=${environment}&secretPath=/${folder.name}`,
          { headers }
        );

        if (!secretsRes.ok) return null;

        const secretsData = await secretsRes.json();
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
      } catch {
        return null;
      }
    });

    // Also fetch root-level secrets (/)
    folderPromises.push(
      (async () => {
        try {
          const rootRes = await fetch(
            `${baseUrl}/api/v3/secrets/raw?workspaceId=${projectId}&environment=${environment}&secretPath=/`,
            { headers }
          );
          if (!rootRes.ok) return null;
          const rootData = await rootRes.json();
          const secrets = (rootData.secrets || [])
            .filter(s => s.secretValue !== undefined && s.secretValue !== '')
            .map(s => ({ key: s.secretKey, value: s.secretValue }))
            .sort((a, b) => a.key.localeCompare(b.key));
          if (secrets.length === 0) return null;
          return { name: 'config', secrets };
        } catch {
          return null;
        }
      })()
    );

    const results = await Promise.all(folderPromises);
    const groups = results
      .filter(Boolean)
      .sort((a, b) => a.name.localeCompare(b.name));

    return Response.json({ success: true, groups });
  } catch (error) {
    return Response.json({ success: false, error: error.message }, { status: 500 });
  }
}
