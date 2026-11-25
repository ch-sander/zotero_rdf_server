export function registerRemoteSingleLoader({
    N3,
    endpointEl,
    remoteMappingIriEl,
    loadRemoteMappingBtn,
    G_MAPPINGS,
    G_ENTITIES,
    MAP_TARGET_P,
    setStatus,
    addQuadsToStores,
    selectMapping,
    selectEntity,
    updateSourceInfo,
    saveSnapshotToIndexedDb,
    clearCachedMapping,
    clearCachedEntity,
    hasLocalSubject
}) {
    async function fetchRemoteConstruct(query, targetGraphIri) {
        const endpoint = (endpointEl.value || "").trim();

        if (!endpoint) {
            throw new Error("Remote endpoint missing.");
        }

        const response = await fetch(endpoint, {
            method: "POST",
            headers: {
                "Content-Type": "application/sparql-query",
                "Accept": "application/n-triples,text/turtle;q=0.9"
            },
            body: query
        });

        if (!response.ok) {
            throw new Error(
                `SPARQL request failed: ${response.status} ${response.statusText}`
            );
        }

        const text = await response.text();
        const contentType =
            (response.headers.get("content-type") || "").toLowerCase();

        const format = contentType.includes("turtle")
            ? "text/turtle"
            : "application/n-triples";

        const parser = new N3.Parser({ format });
        const graph = N3.DataFactory.namedNode(targetGraphIri);

        return parser.parse(text).map(quad =>
            N3.DataFactory.quad(
                quad.subject,
                quad.predicate,
                quad.object,
                graph
            )
        );
    }

    async function fetchRemoteMapping(mappingIri) {
        const query = `
CONSTRUCT {
    <${mappingIri}> ?p ?o .
}
WHERE {
    GRAPH <${G_MAPPINGS}> {
        <${mappingIri}> ?p ?o .
    }
}
`.trim();

        return fetchRemoteConstruct(query, G_MAPPINGS);
    }

    async function fetchRemoteEntity(entityIri) {
        const query = `
CONSTRUCT {
    <${entityIri}> ?p ?o .
    ?nested ?nestedP ?nestedO .
}
WHERE {
    GRAPH <${G_ENTITIES}> {
        <${entityIri}> ?p ?o .

        OPTIONAL {
            <${entityIri}>
                <http://www.opengis.net/ont/geosparql#hasGeometry>
                ?nested .

            ?nested ?nestedP ?nestedO .
        }
    }
}
`.trim();

        return fetchRemoteConstruct(query, G_ENTITIES);
    }

    function findTargetIri(mappingIri, quads) {
        const subject = N3.DataFactory.namedNode(mappingIri);
        const predicate = N3.DataFactory.namedNode(MAP_TARGET_P);

        return quads.find(quad =>
            quad.subject.equals(subject) &&
            quad.predicate.equals(predicate) &&
            quad.object.termType === "NamedNode"
        )?.object.value || null;
    }

    async function loadEntityByIri_simple(entityIri) {
        const iri = String(entityIri || "").trim();

        const entityQuads = await fetchRemoteEntity(iri);

        if (!entityQuads.length) {
            throw new Error("Entity not found.");
        }

        clearCachedEntity?.(iri);
        addQuadsToStores(entityQuads);

        await selectEntity(iri);
        await saveSnapshotToIndexedDb();

        return {
            entityIri: iri,
            quadCount: entityQuads.length
        };
    }

    async function loadMappingByIri(mappingIri) {
        const iri = String(mappingIri || "").trim();

        if (!iri) {
            throw new Error("Mapping IRI missing.");
        }

        const mappingQuads = await fetchRemoteMapping(iri);

        if (!mappingQuads.length) {
            throw new Error("Mapping not found.");
        }

        const targetIri = findTargetIri(iri, mappingQuads);

        if (!targetIri) {
            throw new Error("Mapping has no target entity.");
        }

        const entityQuads = await fetchRemoteEntity(targetIri);

        clearCachedMapping?.(iri);
        clearCachedEntity?.(targetIri);

        addQuadsToStores([
            ...mappingQuads,
            ...entityQuads
        ]);

        updateSourceInfo();
        await selectMapping(iri);
        await saveSnapshotToIndexedDb();

        return {
            mappingIri: iri,
            entityIri: targetIri,
            quadCount: mappingQuads.length + entityQuads.length
        };
    }

    async function fetchRemoteMappingForEntity(entityIri) {
        const query = `
    CONSTRUCT {
        ?mapping ?p ?o .
    }
    WHERE {
        GRAPH <${G_MAPPINGS}> {
            ?mapping <${MAP_TARGET_P}> <${entityIri}> ;
                    ?p ?o .
        }
    }
    `.trim();

        return fetchRemoteConstruct(query, G_MAPPINGS);
    }

    async function loadEntityByIri(entityIri) {
        const iri = String(entityIri || "").trim();

        if (!iri) {
            throw new Error("Entity IRI missing.");
        }

        setStatus("Loading entity…");

        const entityQuads = await fetchRemoteEntity(iri);

        if (!entityQuads.length) {
            throw new Error("Entity not found.");
        }

        setStatus("Resolving mapping…");

        const mappingQuads =
            await fetchRemoteMappingForEntity(iri);

        clearCachedEntity?.(iri);

        if (mappingQuads.length) {
            const mappingIri =
                mappingQuads[0].subject.value;

            clearCachedMapping?.(mappingIri);

            addQuadsToStores([
                ...mappingQuads,
                ...entityQuads
            ]);

            await selectMapping(mappingIri);

            await saveSnapshotToIndexedDb?.();

            return {
                mappingIri,
                entityIri: iri,
                quadCount:
                    mappingQuads.length + entityQuads.length
            };
        }

        addQuadsToStores(entityQuads);

        await selectEntity(iri);
        await saveSnapshotToIndexedDb?.();

        return {
            mappingIri: null,
            entityIri: iri,
            quadCount: entityQuads.length
        };
    }

    loadRemoteMappingBtn.addEventListener("click", async () => {
        try {
            loadRemoteMappingBtn.disabled = true;
            setStatus("Loading mapping and entity…");

            const result = await loadMappingByIri(
                remoteMappingIriEl.value
            );

            setStatus(
                `Loaded ${result.quadCount} quads.`
            );
        } catch (error) {
            console.error(error);
            setStatus(
                error instanceof Error
                    ? error.message
                    : String(error)
            );
        } finally {
            loadRemoteMappingBtn.disabled = false;
        }
    });

    remoteMappingIriEl.addEventListener("keydown", event => {
        if (event.key === "Enter") {
            event.preventDefault();
            loadRemoteMappingBtn.click();
        }
    });

    const params =
        new URLSearchParams(window.location.search);

    const mappingIriFromUrl =
        params.get("mapping");

    const entityIriFromUrl =
        params.get("entity");

    if (mappingIriFromUrl) {
        remoteMappingIriEl.value =
            mappingIriFromUrl;

        const mappingIsLocal =
            hasLocalSubject?.(
                G_MAPPINGS,
                mappingIriFromUrl
            ) === true;

        if (mappingIsLocal) {
            selectMapping(
                mappingIriFromUrl
            ).catch(handleError);
        } else {
            loadMappingByIri(
                mappingIriFromUrl
            ).catch(handleError);
        }
    } else if (entityIriFromUrl) {
        const entityIsLocal =
            hasLocalSubject?.(
                G_ENTITIES,
                entityIriFromUrl
            ) === true;

        if (entityIsLocal) {
            selectEntity(
                entityIriFromUrl
            ).catch(handleError);
        } else {
            loadEntityByIri(
                entityIriFromUrl
            ).catch(handleError);
        }
    }

    function handleError(error) {
        console.error(error);

        setStatus(
            error instanceof Error
                ? error.message
                : String(error)
        );
    }
    return {
        loadMappingByIri,
        loadEntityByIri
    };
}

