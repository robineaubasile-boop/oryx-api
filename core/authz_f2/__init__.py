"""Frontière d'autorisation F2 (R1 PR 5) — DORMANTE.

Implémentation de C.5.2, C.5.2-A, C.5.2-B et des correctifs BF1 / BF2 :

- principal.py : AuthorizedPrincipal (identité serveur immuable, construite
                 depuis les lignes PostgreSQL validées, jamais depuis un
                 user_id client) et AuthorizedContext (principal +
                 T_authorization) ;
- commands.py  : registre FERMÉ de commandes typées (données pures), plans
                 de verrouillage et vues rendues ;
- _adapters.py : adaptateurs SQL privés et audités, seuls détenteurs de la
                 connexion brute (inventaire vérifié par
                 tests/test_authz_f2_boundary.py) ;
- runner.py    : AuthorizedTransactionRunner, unique propriétaire des
                 transactions (phase A de pré-validation, phase B
                 d'autorisation définitive sous verrous, commit ou rollback).

R1 PR 6 : ProfileView porte level_revision ; neuvième commande du
registre fermé, UpdateProfileLevel (CAS sur users.level_revision, plan
PROFILE_MUTATION) ; résultat fermé REVISION_CONFLICT.

La validation du justificatif réutilise core.auth_f2.sessions (même code
que le moteur F2 : aucun second validateur HMAC) et n'écrit jamais : une
session dont la livraison n'est pas confirmée par /session/resolve est
refusée, sans confirmation implicite.

Dormance : aucun module applicatif (api.py, workers, scripts, frontends)
n'importe ce paquet ; il ne lit aucune variable d'environnement, n'ouvre
aucune connexion à l'import et n'appelle aucun réseau. Seuls
core/authz_f2_http et les tests l'importent (tests/test_authz_f2_dormancy.py).
"""
