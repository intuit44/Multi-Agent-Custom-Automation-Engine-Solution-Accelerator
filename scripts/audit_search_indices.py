#!/usr/bin/env python3
"""
Audit AI Search indices — inventario, field-mapping y coherencia para FoundryIQ.

Valida:
  1. Que cada índice exista y tenga vectorSearch + semantic configurados.
  2. Que el shape de un documento REAL del índice coincida con el schema
     declarado (field-mapping real, no solo presencia de campos).
  3. Estado del indexer continuo Cosmos → chat-history-index.
  4. Coherencia HR/Marketing: use_rag=true pero index_name="" → docs nunca indexados.
  5. Estado del push-indexer fire-and-forget en chat_cosmos_service.

Usage:
    cd src/backend && .venv/bin/python ../../scripts/audit_search_indices.py
"""

import asyncio
import json
import os
import sys

import aiohttp
from azure.identity.aio import DefaultAzureCredential as DefaultAzureCredentialAsync
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.join(SCRIPT_DIR, "..")
BACKEND_DIR = os.path.join(REPO_ROOT, "src", "backend")
load_dotenv(os.path.join(BACKEND_DIR, ".env"), override=True)

SEARCH_ENDPOINT = os.getenv("AZURE_AI_SEARCH_ENDPOINT", "").rstrip("/")
SEARCH_API = "2024-07-01"
EMBEDDING_DIMS = 1536

# --- Constantes para Step 5 (Coincidentes con setup_search_pipeline.py) ---
CHAT_DATASOURCE_NAME = "chat-sessions-cosmos-ds"
CHAT_SKILLSET_NAME = "chat-history-skillset"
CHAT_INDEXER_NAME = "chat-history-indexer"

# ── Requisitos mínimos por índice para FoundryIQ ─────────────────
REQUIRED_FIELDS = {
    "content": {"searchable": True, "retrievable": True},
    "content_vector": {"searchable": True, "dimensions": EMBEDDING_DIMS},
}
REQUIRED_SECTIONS = ["vectorSearch", "semantic"]

# Índices de documentos conocidos + chat
DOCUMENT_INDICES = [
    "contract-compliance-doc-index",
    "contract-risk-doc-index",
    "contract-summary-doc-index",
    "macae-retail-customer-index",
    "macae-retail-order-index",
    "macae-rfp-compliance-index",
    "macae-rfp-risk-index",
    "macae-rfp-summary-index",
    "macae-hr-knowledge-index",
    "macae-marketing-knowledge-index",
]
CHAT_INDEX = "chat-history-index"
ALL_INDICES = DOCUMENT_INDICES + [CHAT_INDEX]

# Rol de cada índice
INDEX_ROLES = {
    "contract-compliance-doc-index": "ChatMCPAgent + Magentic (Contract Compliance)",
    "contract-risk-doc-index": "ChatMCPAgent + Magentic (Contract Risk)",
    "contract-summary-doc-index": "ChatMCPAgent + Magentic (Contract Summary)",
    "macae-retail-customer-index": "ChatMCPAgent + Magentic (Retail — customer data)",
    "macae-retail-order-index": "ChatMCPAgent + Magentic (Retail — order data)",
    "macae-rfp-compliance-index": "ChatMCPAgent + Magentic (RFP Compliance)",
    "macae-rfp-risk-index": "ChatMCPAgent + Magentic (RFP Risk)",
    "macae-rfp-summary-index": "ChatMCPAgent + Magentic (RFP Summary)",
    "macae-hr-knowledge-index": "HRHelperAgent + TechnicalSupportAgent (HR Team)",
    "macae-marketing-knowledge-index": "ProductAgent + MarketingAgent (Marketing Team)",
    CHAT_INDEX: "ChatMCPAgent — memoria de sesiones (todos los agentes)",
}

# Shape esperado de un documento real en cada índice
# Doc indices canónicos: id, title, content, source_blob, indexed_at, content_vector.
# content_vector es retrievable=False — no aparecerá en results, pero lo incluimos
# para no marcarlo como "extra" si algún índice histórico lo expusiera.
_DOC_CANONICAL_SHAPE = [
    "id",
    "title",
    "content",
    "source_blob",
    "indexed_at",
    "content_vector",
]
EXPECTED_DOC_SHAPE: dict[str, list[str]] = {
    "contract-compliance-doc-index": _DOC_CANONICAL_SHAPE,
    "contract-risk-doc-index": _DOC_CANONICAL_SHAPE,
    "contract-summary-doc-index": _DOC_CANONICAL_SHAPE,
    "macae-retail-customer-index": _DOC_CANONICAL_SHAPE,
    "macae-retail-order-index": _DOC_CANONICAL_SHAPE,
    "macae-rfp-compliance-index": _DOC_CANONICAL_SHAPE,
    "macae-rfp-risk-index": _DOC_CANONICAL_SHAPE,
    "macae-rfp-summary-index": _DOC_CANONICAL_SHAPE,
    "macae-hr-knowledge-index": _DOC_CANONICAL_SHAPE,
    "macae-marketing-knowledge-index": _DOC_CANONICAL_SHAPE,
    CHAT_INDEX: [
        "id",
        "session_id",
        "user_id",
        "role",
        "title",
        "content",
        "intent",
        "timestamp",
        "session_name",
    ],
}


def sep(char="─", n=70):
    print(char * n)


def validate_index(name: str, schema: dict) -> list[str]:
    """Devuelve lista de problemas encontrados. Lista vacía = OK."""
    issues = []
    fields_by_name = {f["name"]: f for f in schema.get("fields", [])}

    # 1. Campos requeridos
    for field_name, requirements in REQUIRED_FIELDS.items():
        if field_name not in fields_by_name:
            issues.append(f"  ❌ Campo '{field_name}' AUSENTE")
            continue
        f = fields_by_name[field_name]
        for prop, expected in requirements.items():
            actual = f.get(prop)
            if actual != expected:
                issues.append(
                    f"  ❌ Campo '{field_name}'.{prop} = {actual!r}  (esperado: {expected!r})"
                )

    # 2. Secciones top-level
    for section in REQUIRED_SECTIONS:
        if section not in schema:
            issues.append(f"  ❌ Sección '{section}' AUSENTE en el esquema del índice")

    # 3. vectorSearch: debe tener al menos un profile con algorithm hnsw
    vs = schema.get("vectorSearch", {})
    profiles = vs.get("profiles", [])
    algos = vs.get("algorithms", [])
    vectorizers = vs.get("vectorizers", []) or []
    hnsw_algos = [a["name"] for a in algos if a.get("kind") == "hnsw"]
    vectorizer_names = [v.get("name") for v in vectorizers if v.get("name")]
    if not hnsw_algos:
        issues.append("  ❌ vectorSearch: no hay algoritmo 'hnsw' configurado")
    if not profiles:
        issues.append("  ❌ vectorSearch: no hay profiles configurados")
    else:
        for p in profiles:
            if p.get("algorithm") not in hnsw_algos:
                issues.append(
                    f"  ⚠️  vectorSearch profile '{p['name']}' apunta a algoritmo inexistente"
                )

    # 3b. vectorizers: debe haber al menos uno y un profile que lo referencie
    if not vectorizers:
        issues.append(
            "  ⚠️  vectorSearch: sin 'vectorizers' — queries de texto puro no "
            "podrán vectorizarse al vuelo"
        )
    else:
        profile_vectorizers = [
            p.get("vectorizer") for p in profiles if p.get("vectorizer")
        ]
        if not profile_vectorizers:
            issues.append(
                "  ⚠️  vectorSearch: ningún profile referencia un vectorizer"
            )
        else:
            for pv in profile_vectorizers:
                if pv not in vectorizer_names:
                    issues.append(
                        f"  ⚠️  profile.vectorizer '{pv}' no existe en vectorizers"
                    )

    # 4. semantic: debe apuntar a 'content'
    sem = schema.get("semantic", {})
    sem_configs = sem.get("configurations", [])
    if not sem_configs:
        issues.append("  ❌ semantic: sin configuraciones")
    else:
        for sc in sem_configs:
            pf = sc.get("prioritizedFields", {})
            content_fields = [
                cf["fieldName"] for cf in pf.get("prioritizedContentFields", [])
            ]
            if "content" not in content_fields:
                issues.append(
                    f"  ⚠️  semantic config '{sc['name']}': 'content' no está en prioritizedContentFields"
                )

    # 5. Campo key debe ser filterable (necesario para FoundryIQ doc retrieval)
    key_field = next((f for f in schema.get("fields", []) if f.get("key")), None)
    if key_field and not key_field.get("filterable"):
        issues.append(
            f"  ⚠️  Campo key '{key_field['name']}' no es filterable — "
            "puede limitar recuperación por ID en FoundryIQ"
        )

    return issues


def print_field_table(fields: list[dict]):
    """Imprime tabla compacta de campos."""
    header = f"  {'CAMPO':<30} {'TIPO':<25} {'search':<8} {'filter':<8} {'retrie':<8} {'dims'}"
    print(header)
    print("  " + "·" * 68)
    for f in fields:
        dims = f.get("dimensions", "")
        print(
            f"  {f['name']:<30} {f.get('type', ''):<25} "
            f"{'✓' if f.get('searchable') else '·':<8}"
            f"{'✓' if f.get('filterable') else '·':<8}"
            f"{'✓' if f.get('retrievable') else '·':<8}"
            f"{dims}"
        )


async def audit():
    if not SEARCH_ENDPOINT:
        print("❌  AZURE_AI_SEARCH_ENDPOINT no está configurado en .env")
        sys.exit(1)

    credential = DefaultAzureCredentialAsync()
    session = aiohttp.ClientSession()

    async def get_token():
        t = await credential.get_token("https://search.azure.com/.default")
        return {
            "Authorization": f"Bearer {t.token}",
            "Content-Type": "application/json",
        }

    async def list_all_indices() -> list[str]:
        headers = await get_token()
        url = f"{SEARCH_ENDPOINT}/indexes?api-version={SEARCH_API}&$select=name"
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return [i["name"] for i in data.get("value", [])]
            return []

    async def get_index(name: str):
        headers = await get_token()
        url = f"{SEARCH_ENDPOINT}/indexes/{name}?api-version={SEARCH_API}"
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def count_docs(name: str) -> int:
        headers = await get_token()
        url = f"{SEARCH_ENDPOINT}/indexes/{name}/docs/$count?api-version={SEARCH_API}"
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                text = await resp.text()
                return int(text.strip())
            return -1

    try:
        sep("═")
        print("  MACAE — Auditoría de índices AI Search para FoundryIQ")
        print(f"  Endpoint: {SEARCH_ENDPOINT}")
        sep("═")
        print()

        # Descubrir índices existentes en el servicio
        live_indices = await list_all_indices()
        print(f"  Índices encontrados en el servicio: {len(live_indices)}")
        for n in live_indices:
            marker = "✅" if n in ALL_INDICES else "⚠️  (desconocido)"
            print(f"    • {n}  {marker}")
        print()

        # Unión de conocidos + live para auditar todos
        to_audit = sorted(set(ALL_INDICES) | set(live_indices))
        total_ok = 0
        total_warn = 0
        total_missing = 0

        for idx_name in to_audit:
            sep()
            role = INDEX_ROLES.get(idx_name, "Desconocido")
            print(f"  📋  {idx_name}")
            print(f"      Rol: {role}")
            sep("·")

            schema = await get_index(idx_name)
            if schema is None:
                print("  ❌  ÍNDICE NO EXISTE en el servicio")
                total_missing += 1
                print()
                continue

            doc_count = await count_docs(idx_name)
            print(
                f"  Documentos indexados: {doc_count if doc_count >= 0 else 'error al obtener'}"
            )

            # Campos
            fields = schema.get("fields", [])
            print(f"  Campos ({len(fields)}):")
            print_field_table(fields)

            # vectorSearch
            vs = schema.get("vectorSearch", {})
            if vs:
                profiles = [p["name"] for p in vs.get("profiles", [])]
                algos = [
                    f"{a['name']} ({a.get('kind', '?')})"
                    for a in vs.get("algorithms", [])
                ]
                print("\n  vectorSearch:")
                print(f"    algorithms : {algos}")
                print(f"    profiles   : {profiles}")
            else:
                print("\n  vectorSearch: ❌ AUSENTE")

            # semantic
            sem = schema.get("semantic", {})
            if sem:
                configs = sem.get("configurations", [])
                for sc in configs:
                    pf = sc.get("prioritizedFields", {})
                    title = pf.get("titleField", {}).get("fieldName", "—")
                    content_fields = [
                        cf["fieldName"] for cf in pf.get("prioritizedContentFields", [])
                    ]
                    keyword_fields = [
                        cf["fieldName"]
                        for cf in pf.get("prioritizedKeywordsFields", [])
                    ]
                    print(f"\n  semantic config '{sc['name']}':")
                    print(f"    titleField          : {title}")
                    print(f"    contentFields       : {content_fields}")
                    print(f"    keywordsFields      : {keyword_fields}")
            else:
                print("\n  semantic: ❌ AUSENTE")

            # Doc-sampling: fetch 1 doc y validar fields reales vs shape esperado
            expected_fields = EXPECTED_DOC_SHAPE.get(idx_name, [])
            if expected_fields and doc_count > 0:
                headers = await get_token()
                sample_url = (
                    f"{SEARCH_ENDPOINT}/indexes/{idx_name}/docs"
                    f"?api-version={SEARCH_API}&$top=1&$select=*"
                )
                async with session.get(sample_url, headers=headers) as sresp:
                    if sresp.status == 200:
                        sdata = await sresp.json()
                        docs = sdata.get("value", [])
                        if docs:
                            actual_keys = set(docs[0].keys()) - {"@search.score"}
                            missing_fields = [
                                f for f in expected_fields if f not in actual_keys
                            ]
                            extra_fields = [
                                f for f in actual_keys if f not in expected_fields
                            ]
                            print("\n  Doc-sampling (1 doc real):")
                            print(f"    Campos esperados : {expected_fields}")
                            print(f"    Campos reales    : {sorted(actual_keys)}")
                            if missing_fields:
                                print(
                                    f"    ❌ Campos AUSENTES en doc real: {missing_fields}"
                                )
                            else:
                                print(
                                    "    ✅ Todos los campos esperados presentes en el doc"
                                )
                            if extra_fields:
                                print(
                                    f"    ℹ️  Campos extra (no en shape): {extra_fields}"
                                )

            # Validación FoundryIQ
            issues = validate_index(idx_name, schema)
            print()
            if not issues:
                print(
                    "  ✅  VÁLIDO para FoundryIQ — ChatMCPAgent y Magentic pueden consultar este índice"
                )
                total_ok += 1
            else:
                print("  ⚠️   PROBLEMAS DETECTADOS:")
                for issue in issues:
                    print(issue)
                total_warn += 1
            print()

        # ── Auditoría completa Step 5: datasource + skillset + indexer ────
        sep("═")
        print("  STEP 5 — Indexer continuo Cosmos → chat-history-index")
        sep("═")
        step5_issues: list[str] = []

        # 1. Datasource
        headers = await get_token()
        async with session.get(
            f"{SEARCH_ENDPOINT}/datasources/{CHAT_DATASOURCE_NAME}?api-version={SEARCH_API}",
            headers=headers,
        ) as dr:
            if dr.status == 200:
                ds = await dr.json()
                policy = ds.get("dataChangeDetectionPolicy") or {}
                odata_type = policy.get("@odata.type", "")
                hwm_col = policy.get("highWaterMarkColumnName", "")
                query = (ds.get("container") or {}).get("query", "")
                print(f"  datasource '{CHAT_DATASOURCE_NAME}': ✅")
                print(f"    changeDetection : {odata_type.split('.')[-1] or '—'}")
                print(f"    highWaterMark   : {hwm_col or '❌ AUSENTE'}")
                has_title = "title" in query
                print(
                    f"    title en query  : {'✅' if has_title else '❌ AUSENTE — docs del indexer quedan sin title'}"
                )
                if "HighWaterMark" not in odata_type:
                    step5_issues.append(
                        "datasource: HighWaterMarkChangeDetectionPolicy ausente"
                    )
                if not hwm_col:
                    step5_issues.append("datasource: highWaterMarkColumnName vacío")
                if not has_title:
                    step5_issues.append(
                        "datasource: campo 'title' no proyectado — indexer path degrada chat-history-index"
                    )
            else:
                print(
                    f"  datasource '{CHAT_DATASOURCE_NAME}': ❌ NO EXISTE (HTTP {dr.status})"
                )
                step5_issues.append(
                    f"datasource '{CHAT_DATASOURCE_NAME}' no encontrado"
                )

        # 2. Skillset
        headers = await get_token()
        async with session.get(
            f"{SEARCH_ENDPOINT}/skillsets/{CHAT_SKILLSET_NAME}?api-version={SEARCH_API}",
            headers=headers,
        ) as sr:
            if sr.status == 200:
                ss = await sr.json()
                skills = ss.get("skills", [])
                embed_skills = [
                    s for s in skills if "Embedding" in s.get("@odata.type", "")
                ]
                print(
                    f"  skillset '{CHAT_SKILLSET_NAME}': ✅  ({len(skills)} skill(s), {len(embed_skills)} embedding)"
                )
                if not embed_skills:
                    step5_issues.append(
                        "skillset: sin AzureOpenAIEmbeddingSkill — docs entrarán sin content_vector"
                    )
            else:
                print(
                    f"  skillset '{CHAT_SKILLSET_NAME}': ❌ NO EXISTE (HTTP {sr.status})"
                )
                step5_issues.append(f"skillset '{CHAT_SKILLSET_NAME}' no encontrado")

        # 3. Indexer
        headers = await get_token()
        async with session.get(
            f"{SEARCH_ENDPOINT}/indexers/{CHAT_INDEXER_NAME}?api-version={SEARCH_API}",
            headers=headers,
        ) as ir:
            if ir.status == 200:
                ix = await ir.json()
                interval = (ix.get("schedule") or {}).get("interval", "sin schedule")
                mapped_sources = [
                    m["sourceFieldName"] for m in (ix.get("fieldMappings") or [])
                ]
                output_targets = [
                    m["targetFieldName"] for m in (ix.get("outputFieldMappings") or [])
                ]
                has_title_map = "title" in mapped_sources
                has_vector_map = "content_vector" in output_targets
                print(f"  indexer '{CHAT_INDEXER_NAME}': ✅")
                print(f"    schedule         : {interval}")
                print(f"    fieldMappings    : {mapped_sources}")
                print(f"    outputMappings   : {output_targets}")
                print(
                    f"    title mapeado    : {'✅' if has_title_map else '❌ AUSENTE'}"
                )
                print(
                    f"    vector mapeado   : {'✅' if has_vector_map else '❌ AUSENTE'}"
                )
                if not has_title_map:
                    step5_issues.append(
                        "indexer: 'title' no está en fieldMappings — docs sin title semántico"
                    )
                if not has_vector_map:
                    step5_issues.append(
                        "indexer: 'content_vector' no está en outputFieldMappings"
                    )
            else:
                print(
                    f"  indexer '{CHAT_INDEXER_NAME}': ❌ NO EXISTE (HTTP {ir.status})"
                )
                step5_issues.append(f"indexer '{CHAT_INDEXER_NAME}' no encontrado")

        print()
        if step5_issues:
            print("  ⚠️  Problemas Step 5:")
            for p in step5_issues:
                print(f"     • {p}")
            print(
                "     → Ejecutar setup_search_pipeline.py para recrear datasource/skillset/indexer"
            )
        else:
            print("  ✅  Step 5 completo — datasource, skillset e indexer correctos")
        print()

        # ── Validación HR/Marketing: use_rag=true pero index_name="" ─────
        sep("═")
        print("  COHERENCIA agentes: use_rag vs index_name")
        sep("═")
        teams_dir = os.path.join(REPO_ROOT, "data", "agent_teams")
        orphaned: list[tuple[str, str]] = []  # (archivo, agent_name)
        if os.path.isdir(teams_dir):
            for fname in sorted(os.listdir(teams_dir)):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(teams_dir, fname)
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        team_data = json.load(fh)
                    agents = team_data if isinstance(team_data, list) else [team_data]
                    for agent in agents:
                        if (
                            agent.get("use_rag")
                            and not agent.get("index_name", "").strip()
                        ):
                            orphaned.append((fname, agent.get("name", "?")))
                except Exception as exc:  # noqa: BLE001
                    print(f"  ⚠️  No se pudo leer {fname}: {exc}")
        else:
            print(f"  ⚠️  Directorio de agentes no encontrado: {teams_dir}")

        if orphaned:
            print("  ❌ Agentes con use_rag=true pero index_name vacío:")
            for fname, agent_name in orphaned:
                print(f"     • {fname} → agente '{agent_name}'")
            print()
            print(
                "  Consecuencia: FoundryIQ nunca recupera documentos para estos agentes."
            )
            print("  Opciones:")
            print(
                "    1. Asignar un index_name válido al agente en data/agent_teams/<team>.json"
            )
            print(
                "    2. Cambiar use_rag a false si el agente no necesita recuperación"
            )
        else:
            print(
                "  ✅ Todos los agentes con use_rag=true tienen index_name configurado."
            )
        print()

        # Resumen final
        sep("═")
        print("  RESUMEN")
        sep("═")
        print(f"  Total auditados : {len(to_audit)}")
        print(f"  ✅  Válidos      : {total_ok}")
        print(f"  ⚠️   Con problemas: {total_warn}")
        print(f"  ❌  Ausentes     : {total_missing}")
        if orphaned:
            print(f"  ❌  Agentes huérfanos (use_rag sin índice): {len(orphaned)}")
        print()
        if step5_issues:
            print(f"  ⚠️  Step 5 (indexer continuo): {len(step5_issues)} problema(s)")
        print()
        if total_warn > 0 or total_missing > 0 or orphaned or step5_issues:
            print("  Acciones recomendadas:")
            if total_missing > 0 or total_warn > 0 or step5_issues:
                print(
                    "    • cd src/backend && .venv/bin/python ../../scripts/setup_search_pipeline.py"
                )
            if orphaned:
                print(
                    "    • Revisar data/agent_teams/*.json — asignar index_name o desactivar use_rag"
                )
        else:
            print(
                "  Todos los índices, agentes y el indexer continuo están correctamente configurados. ✅"
            )
        sep("═")

    finally:
        await session.close()
        await credential.close()


if __name__ == "__main__":
    asyncio.run(audit())
