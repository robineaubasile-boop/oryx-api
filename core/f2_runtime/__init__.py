"""Ressources d'exécution F2 (ACT-L1 / F04-B V0.3) — DORMANTES.

Quatre modules, tous à appel explicite, sans aucun effet à l'import :

- config     : chargeur PUR de la configuration stricte F2 (B01) ;
- database   : fabrique du moteur SQLAlchemy synchrone F2 (B03, B04) ;
- resources  : propriété et fermeture des ressources construites (B05) ;
- preflight  : précontrôles PostgreSQL en lecture seule (B06).

Aucun module applicatif (api.py, workers, scripts) ne les importe ; aucun
point d'entrée ne les utilise. La composition ASGI, le lifespan et le suivi
des travaux engagés relèvent de F04-C ; voir docs/act_l1_f04b_resources_v03.md.
"""
