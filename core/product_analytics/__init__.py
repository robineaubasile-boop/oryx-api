"""Services métier DORMANTS de l'analytics produit (V0-T08 Lots 2A et 2B-1).

Paquet isolé, posé sur le socle PostgreSQL des migrations
0022_product_analytics_v1 (tables product_campaigns,
product_campaign_participants, product_activity_events) et
0023_analytics_cohort_sessions (product_campaign_cohorts,
product_cohort_seats, product_activity_sessions,
product_activity_session_interactions) :

- common.py      : erreurs, vocabulaires fermés, registre des politiques,
                   validation structurelle, horloge serveur ;
- campaigns.py   : création explicite et transitions contrôlées des campagnes
                   (planned -> active -> closed, planned/active -> cancelled),
                   politiques immuables après activation ;
- participation.py : inscription Vague 0 à la première connexion éligible
                   (J0 serveur, fenêtre [J0, J0 + 12 j) exacte), inscription
                   à fenêtre explicite (scénarios contrôlés) et retrait ;
- retention.py   : validation métier du document retention-scopes-v1 et de
                   ses deux colonnes d'échéance ; règle de révision (aucune
                   prolongation, aucune finalité rétroactive) ;
- events.py      : validation et persistance idempotente des événements ;
- cohort.py      : lot 2B-1 — cohorte pré-J0 (cinq places, invitation F2
                   canonique, rattachement par preuve F2 committed,
                   autorisation explicite), date limite d'admission,
                   activation d'une campagne à cohorte, J0 individuel
                   (réutilise enroll_at_first_eligible_login) et contrat non
                   bloquant ;
- sessions.py    : lot 2B-1 — sessions d'activité produit (30 minutes
                   d'inactivité, horloge serveur, environnements distincts,
                   registre des clés d'interaction : tout rejeu idempotent,
                   clôture volontaire, finalisation sans événement
                   inventé), distinctes des sessions F2 ;
- proofs.py      : lot 2B-2 bloc B — émission de l'origine d'une opération
                   (interaction 2B-1 NOUVELLE, clé serveur, liée à cette seule
                   opération), registre provisoire des preuves
                   d'utilisation significative (pending idempotent) et
                   certification contrôlée pending -> certified après le
                   COMMIT de l'opération métier (horloge PostgreSQL lue après
                   les verrous, fenêtre [J0, J+12) stricte), rejets et
                   incertitudes terminaux ; aucun événement ni attribution
                   définitive (bloc C). Seul importateur : la frontière
                   privée hors du paquet, elle-même sans appelant.

DORMANCE : aucun module applicatif n'importe ce paquet (api.py, routes,
workers, scripts restent inchangés ; tests/test_product_analytics_dormancy.py),
à l'exception nominative de la frontière privée du bloc B (proofs.py
seulement), qui n'a elle-même aucun appelant.
Ce fichier n'importe rien : importer le paquet n'importe aucun service. Les
services ne font jamais commit() ni rollback() de la transaction de
l'appelant ; ils ne lisent aucune configuration et n'ouvrent aucune
connexion. cohort.py lit (sans écrire ni verrouiller) les tables F2
invitations et auth_operations, sans importer aucun module F2.
Suppression effective, purges planifiées et rapports : lot 2C.
"""
