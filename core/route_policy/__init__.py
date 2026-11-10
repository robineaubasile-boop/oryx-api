"""Politique de routage fermée F-02A V0.2 — DORMANTE.

Paquet isolé, destiné à la future composition ASGI F-04 :

- registry.py  : registre FERMÉ et figé des couples (méthode, gabarit) connus,
                 avec leur catégorie (legacy retiré, désactivé, public candidat,
                 F2 existant, documentation automatique du framework) ;
- canonical.py : forme canonique du chemin BRUT (raw_path) et appariement
                 exact aux gabarits, sans aucun décodage ;
- gate.py      : décision pure (decide) et porte ASGI (RoutePolicyGate) qui
                 produit elle-même les refus (410 / 404 / 405) et ne transmet
                 qu'aux applications F2 explicitement injectées.

Aucun module applicatif n'importe ce paquet : api.py (le véritable point
d'entrée) reste strictement inchangé (tests/test_route_policy_dormancy.py).
Le paquet n'importe ni api, ni FastAPI, ni Starlette, ni base de données, ni
fournisseur, et ne lit aucune configuration.
"""
