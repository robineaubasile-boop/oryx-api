"""Route personnelle isolée GET /api/v2/me (R1 PR 5) — DORMANTE.

Adaptateur HTTP autour de core.authz_f2.AuthorizedTransactionRunner
(C.5.2 §7) :

- provenance.py : politique propre aux GET personnels (Host canonique et
                  autorisé, Origin exact s'il est présent, sinon
                  Sec-Fetch-Site: same-origin obligatoire, Referer cohérent,
                  en-têtes dupliqués refusés) ; celle de PR 3 est inchangée ;
- routes.py     : GET /api/v2/me (sélecteur X-Oryx-Generation-Id, cookie
                  __Host-oryx-active-<generation_id> exact lu par le parseur
                  strict de PR 3, ni query ni corps, réponses no-store, aucun
                  Set-Cookie) ;
- isolated.py   : application personnelle ISOLÉE (tests uniquement).

Dormance : api.py n'importe ni ce paquet ni core.authz_f2 ; GET /api/v2/me
n'existe dans aucune application publique, quelles que soient les
variables d'environnement (tests/test_authz_f2_dormancy.py). La
composition réunissant les routes F2 et cette route n'existe que dans les
tests (tests/authz_f2_support.py).
"""
