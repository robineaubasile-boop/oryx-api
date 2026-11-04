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

R1 PR 7 : portefeuille versionné et idempotent. Registre fermé à onze
commandes (UpdatePositionQuantity retirée ; CreatePortfolioPosition,
ReplacePortfolioPosition, DeletePortfolioPosition ajoutées, plan
PORTFOLIO_MUTATION sous READ COMMITTED vérifié), résultat fermé
IDEMPOTENCY_CONFLICT, et trois modules purs :

- portfolio_values.py      : saisie décimale, format binary64-decimal-v1,
                             diagnostic des lignes historiques ;
- portfolio_fingerprint.py : trousseau HMAC portefeuille dédié et trame
                             oryx-portfolio-request-v1 ;
- portfolio_results.py     : résultats et reçus portfolio-write-result-v1.

R1 PR 8 : registre fermé à quatorze commandes (ListAnalysisOverviewPage,
ListThesesPage, ReadThesis ajoutées sous le plan READ inchangé ;
ListTheses et ListThesesForTicker conservées) ; une instruction SQL
métier par lecture, sans verrou métier ni écriture.

R1 PR 9-A : plan DECRYPTAGE_MUTATION (users FOR NO KEY UPDATE) et pont
ORM privé _decryptage_orm_bridge.py (seul module autorisé à construire une
Session, sur la connexion du runner, join_transaction_mode rollback_only),
sans aucune commande de production : le registre reste à quatorze
commandes ; le pont n'est exercé que par le banc d'essai isolé des tests.

R1 PR 9-B1 : réservation durable des tours Décrypter. Registre fermé à
seize commandes (ReserveDecryptageTurn, RecordDecryptageAttemptFailure,
plan DECRYPTAGE_MUTATION inchangé, sans verrou préparatoire), table
decryptage_turn_reservations (migration 0021), bail, epoch et fencing ;
dépendance facultative et dédiée decryptage_keyring du runner
(Spec.decryptage_keyed, distinct de Spec.keyed). Deux modules purs :

- decryptage_fingerprint.py : DecryptageKeyring dédié, trame
                              oryx-decryptage-reservation-v1, admission
                              structurelle ;
- decryptage_reservation.py : politique de bail figée, LeaseToken,
                              capacité DecryptageLease et vues fermées.

Aucune route, aucun appel Claude ni EODHD, aucune écriture cognitive.

R1 PR 9-B2 : préparation et génération DORMANTES d'un candidat interne.
Registre fermé à dix-sept commandes (ReadDecryptagePreparationContext,
plan READ inchangé, trousseau Décrypter par le dispatch existant) ; cinq
modules :

- decryptage_context.py     : vue de préparation fermée (pure) ;
- decryptage_instrument.py  : identification de l'instrument (pure) ;
- decryptage_market_data.py : politique EODHD / FMP fermée (pure, sans
                              yfinance) ;
- decryptage_candidate.py   : snapshot versionné, validation de la réponse,
                              GeneratedDecryptageCandidate ;
- decryptage_generation.py  : orchestrateur et budget (transports injectés,
                              aucun réseau importé).

Aucune route, aucune AssistantDelivery, aucune finalisation, aucune écriture
cognitive ; les transports réels (core/decryptage_providers.py) ne sont
importés par aucun module applicatif.

R1 PR 9-C1-B : finalisation atomique DORMANTE. Registre fermé à dix-huit
commandes (FinalizeDecryptageCandidate, plan DECRYPTAGE_MUTATION inchangé,
trousseau Décrypter par le dispatch existant) ; inventaire du pont privé à
trois opérations (CandidateFinalization). Trois modules :

- _bridge_contracts.py           : contrats communs PURS (sentinelles,
                                   AdapterInvariantBroken, capacités scellées
                                   TransactionProbe / FinalizationReadPort) ;
                                   les adaptateurs SQL importent le pont, qui
                                   n'importe plus les adaptateurs (F-01) ;
- decryptage_method_selection.py : sélecteur de méthode PUR partagé par la
                                   génération B2 et la revalidation C1-B ;
- decryptage_finalization.py     : vues fermées de finalisation et projection
                                   financière pure.

Une transaction : verrou B1 propriétaire, HMAC, epoch, jeton, échéance ->
pont (SAVEPOINT C1, claim, Prepare, relecture B2, comparaison, Apply,
RELEASE) -> UPDATE reserved -> finalized -> COMMIT unique. Aucune route,
aucun appel externe, aucune migration.

La validation du justificatif réutilise core.auth_f2.sessions (même code
que le moteur F2 : aucun second validateur HMAC) et n'écrit jamais : une
session dont la livraison n'est pas confirmée par /session/resolve est
refusée, sans confirmation implicite.

Dormance : aucun module applicatif (api.py, workers, scripts, frontends)
n'importe ce paquet ; il ne lit aucune variable d'environnement, n'ouvre
aucune connexion à l'import et n'appelle aucun réseau. Seuls
core/authz_f2_http et les tests l'importent (tests/test_authz_f2_dormancy.py).
"""
