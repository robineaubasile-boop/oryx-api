"""Services métier DORMANTS de l'analytics produit (V0-T08 Lot 2A).

Paquet isolé, posé sur le socle PostgreSQL de la migration
0022_product_analytics_v1 (tables product_campaigns,
product_campaign_participants, product_activity_events) :

- common.py      : erreurs, vocabulaires fermés, registre des politiques,
                   validation structurelle, horloge serveur ;
- campaigns.py   : création explicite et transitions contrôlées des campagnes
                   (planned -> active -> closed, planned/active -> cancelled),
                   politiques immuables après activation ;
- participation.py : inscription et retrait des participants, fenêtre
                   d'observation explicite ;
- retention.py   : validation métier du document retention-scopes-v1 et de
                   ses deux colonnes d'échéance ; règle de révision (aucune
                   prolongation, aucune finalité rétroactive) ;
- events.py      : validation et persistance idempotente des événements.

DORMANCE : aucun module applicatif n'importe ce paquet (api.py, routes,
workers, scripts restent inchangés ; tests/test_product_analytics_dormancy.py).
Ce fichier n'importe rien : importer le paquet n'importe aucun service. Les
services ne font jamais commit() ni rollback() de la transaction de
l'appelant ; ils ne lisent aucune configuration et n'ouvrent aucune
connexion. Suppression effective, purges planifiées et rapports : lot 2C.
"""
