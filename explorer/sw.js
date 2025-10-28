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

function termToSRJ(t){
  if (t.termType === 'NamedNode') return { type:'uri', value:t.value };
  if (t.termType === 'BlankNode') return { type:'bnode', value:t.value };
  // Literal
  const base = { type:'literal', value:t.value };
  if (t.language) return { ...base, 'xml:lang': t.language };
  const dt = t.datatype && t.datatype.value;
  return (dt && dt !== 'http://www.w3.org/2001/XMLSchema#string') ? { ...base, datatype: dt } : base;
}

async function handleSparql(req){
  const url = new URL(req.url);
  const ds = url.searchParams.get('ds');
  if (!ds) {
    return new Response(JSON.stringify({ error:'missing ?ds' }), {
      status:400, headers:{ 'content-type':'application/json; charset=utf-8' }
    });
  }
  const datasetUrl = new URL(ds, self.registration.scope).href;
  const query = req.method === 'GET' ? (url.searchParams.get('query') || '') : await req.text();

  try {
    const engine = new self.Comunica.QueryEngine();
    const bindingsStream = await engine.queryBindings(query, {
      sources: [datasetUrl],
      unionDefaultGraph: true
    });

    const vars = new Set();
    const rows = [];

    await new Promise((resolve, reject) => {
      bindingsStream.on('data', b => {
        const vts = b.variables ?? b._variables ?? [];
        for (const v of vts) vars.add(v.value || v);
        const row = {};
        if (typeof b.forEach === 'function') {
          b.forEach((term, name) => { row[(name.value || name)] = termToSRJ(term); });
        } else {
          for (const v of vts) {
            const t = b.get(v);
            if (t) row[(v.value || v)] = termToSRJ(t);
          }
        }
        rows.push(row);
      });
      bindingsStream.on('end', resolve);
      bindingsStream.on('error', reject);
    });

    const varList = vars.size ? Array.from(vars) : Object.keys(rows[0] || {});
    const body = JSON.stringify({ head: { vars: varList }, results: { bindings: rows } });

    return new Response(body, {
      headers: { 'content-type':'application/sparql-results+json; charset=utf-8' }
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }), {
      status:500, headers:{ 'content-type':'application/json; charset=utf-8' }
    });
  }
}
