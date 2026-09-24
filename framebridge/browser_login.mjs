// Secrets stay in memory until the Python receiver saves platform-local storage.
export async function login({ context, getCDPSession, endpoint, nonce }) {
  const page = await context.newPage();
  let captured;
  const listener = request => {
    if (request.url() !== 'https://api.frame.io/graphql') return;
    const headers = request.headers();
    if (headers.authorization && headers['apollographql-client-name'] && headers['apollographql-client-version']) captured = headers;
  };
  page.on('request', listener);
  try {
    await page.goto('https://next.frame.io', { waitUntil: 'domcontentloaded' });
    const deadline = Date.now() + 120000;
    while (!captured && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 500));
    if (!captured) throw new Error('No authenticated Frame.io request. Sign in and retry.');
    const cdp = await getCDPSession({ page });
    const { cookies } = await cdp.send('Network.getCookies', { urls: ['https://next.frame.io'] });
    const cookie = name => cookies.find(c => c.name === name)?.value;
    const access = captured.authorization.replace(/^Bearer\s+/i, '');
    const refresh = cookie('refreshTokenId');
    const session = cookie('sessionToken');
    if (!refresh || !session) throw new Error('Renewable session cookies unavailable. Sign in again.');
    let expires = 0;
    try { expires = JSON.parse(Buffer.from(access.split('.')[1], 'base64url').toString()).exp || 0; } catch {}
    const response = await fetch(endpoint, { method: 'POST', headers: { 'content-type': 'application/json', 'x-login-nonce': nonce },
      body: JSON.stringify({ access_token: access, refresh_token: decodeURIComponent(refresh), session_token: decodeURIComponent(session),
        client_name: captured['apollographql-client-name'], client_version: captured['apollographql-client-version'], expires_at: expires }) });
    if (!response.ok) throw new Error('Local session receiver failed.');
    console.log('Session captured into local credential storage.');
  } finally {
    page.off('request', listener);
    captured = undefined;
    await page.close();
  }
}
