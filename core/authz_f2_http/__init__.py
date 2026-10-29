"""Routes personnelles isolées GET /api/v2/me (R1 PR 5),
PUT /api/v2/me/level (R1 PR 6), portefeuille (R1 PR 7) et lectures des
analyses et des thèses (R1 PR 8) — DORMANTES.

Adaptateur HTTP autour de core.authz_f2.AuthorizedTransactionRunner
(C.5.2 §7) :

- provenance.py : politique propre aux GET personnels (Host canonique et
                  autorisé, Origin exact s'il est présent, sinon
                  Sec-Fetch-Site: same-origin obligatoire, Referer cohérent,
                  en-têtes dupliqués refusés ; R1 PR 6 : Origin obligatoire
                  et exact pour les mutations) ; celle de PR 3 est inchangée ;
- routes.py     : GET /api/v2/me (sélecteur X-Oryx-Generation-Id, cookie
                  __Host-oryx-active-<generation_id> exact lu par le parseur
                  strict de PR 3, ni query ni corps, réponses no-store, aucun
                  Set-Cookie) et PUT /api/v2/me/level (Origin obligatoire,
                  CSRF oryx-f2-csrf-v1 de PR 3, corps JSON exact, CAS sur
                  level_revision, 409 level_revision_conflict) ;
- portfolio.py  : R1 PR 7, GET / POST /api/v2/me/portfolio et GET / PUT /
                  DELETE /api/v2/me/portfolio/{position_id} (CRUD versionné
                  et idempotent, X-Oryx-Operation-Id, X-Oryx-Expected-Revision,
                  corps canoniques rejoués octet pour octet) ;
- consultation.py, consultation_codec.py : R1 PR 8, GET
                  /api/v2/me/analysis-overview, GET /api/v2/me/theses et GET
                  /api/v2/me/theses/{thesis_id} (lectures seules, plan READ,
                  query stricte, curseurs versionnés Base64URL canoniques,
                  bornes de restitution 131072 / 512 octets, aucune
                  troncature) ;
- isolated.py   : application personnelle ISOLÉE (tests uniquement).

Dormance : api.py n'importe ni ce paquet ni core.authz_f2 ; GET /api/v2/me,
PUT /api/v2/me/level, les routes /api/v2/me/portfolio,
/api/v2/me/analysis-overview et /api/v2/me/theses n'existent dans aucune
application publique, quelles que soient les variables d'environnement
(tests/test_authz_f2_dormancy.py). La
composition réunissant les routes F2 et cette route n'existe que dans les
tests (tests/authz_f2_support.py).
"""
