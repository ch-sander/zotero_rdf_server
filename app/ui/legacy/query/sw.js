/* /explorer/sw.js */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
importScripts('/explorer/js/comunica-browser.js');

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const scopePath = new URL(self.registration.scope).pathname.replace(/\/+$/, '');
  const sparqlPath = scopePath + '/sparql';
  if (url.pathname === sparqlPath || url.pathname.startsWith(sparqlPath)) {
    event.respondWith(handleSparql(event.request));
  }
});

function toWebBody(data) {
  if (typeof data === 'string') return data;
  if (data && typeof data.getReader === 'function') return data;
  if (data && typeof data.on === 'function') {
    return new ReadableStream({
      start(controller) {
        data.on('data', chunk => controller.enqueue(
          typeof chunk === 'string' ? new TextEncoder().encode(chunk) : chunk
        ));
        data.on('end', () => controller.close());
        data.on('error', e => controller.error(e));
      }
    });
  }
  return JSON.stringify(data);
}

async function handleSparql(req) {
  const url = new URL(req.url);
  const ds = url.searchParams.get('ds');
  if (!ds) {
    return new Response(JSON.stringify({ error: 'missing ?ds' }), {
      status: 400, headers: { 'content-type': 'application/json; charset=utf-8' }
    });
  }
  const datasetUrl = new URL(ds, self.registration.scope).href;
  const query = req.method === 'GET' ? (url.searchParams.get('query') || '') : await req.text();

  try {
    const engine = new self.Comunica.QueryEngine();

    const result = await engine.query(query, {
      sources: [datasetUrl],
      unionDefaultGraph: true
    });

    const { data, mediaType } = await engine.resultToString(
      result,
      'application/sparql-results+json'
    );

    return new Response(toWebBody(data), {
      headers: { 'content-type': (mediaType || 'application/sparql-results+json') + '; charset=utf-8' }
    });

  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }), {
      status: 500, headers: { 'content-type': 'application/json; charset=utf-8' }
    });
  }
}