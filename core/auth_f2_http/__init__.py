"""Couche HTTP F2 (R1 PR 3) — DORMANTE.

Adaptateur HTTP autour de core.auth_f2.service.F2AuthService (C.3.3,
annexe C.3.3-A, compléments A18 à A20). Il ne réimplémente ni l'OTP, ni
les quotas, ni la cryptographie F2, ni les sessions, ni les transactions
de PR 2 : il valide le transport (schéma, tailles, origine, CSRF, IP,
cookies), sélectionne explicitement le justificatif désigné, appelle le
moteur et traduit son résultat par une matrice fermée
(core.auth_f2_http.contract).

Modules :

- cookies.py    : noms __Host-oryx-*, lecture stricte de l'en-tête Cookie,
                  écriture des Set-Cookie (Secure, HttpOnly, Path=/,
                  SameSite=Lax, sans Domain) ;
- csrf.py       : protocole de test oryx-f2-csrf-v1 (clés propres,
                  versionnées, indépendantes des six clés F2) ;
- provenance.py : contrôle d'origine (Origin exact, ou même origine
                  attestée par Sec-Fetch-Site) ;
- client_ip.py  : résolveurs d'IP injectables, refus fermé ;
- contract.py   : matrice (endpoint, Outcome.status, Outcome.reason) ;
- contract_pr4.py : matrice distincte des sept routes de R1 PR 4
                  (invitation, activation, reprise, liaison e-mail) ;
- routes.py     : routes /api/auth/f2/* (admission HTTP de R1 PR 4 :
                  core.auth_f2.http_admission, injectée) ;
- isolated.py   : composition d'application ISOLÉE (tests uniquement).

Dormance : api.py (le véritable point d'entrée) n'importe ni ce paquet ni
core.auth_f2 ; aucune route /api/auth/f2 n'existe en production, quelles
que soient les variables d'environnement (tests/test_f2_dormancy.py,
tests/test_f2_http_dormancy.py). Ce paquet ne lit aucune variable
d'environnement : tout est injecté par l'appelant.
"""
