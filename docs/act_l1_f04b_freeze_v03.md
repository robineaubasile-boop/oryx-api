<!--
TRANSCRIPTION — en-tête de provenance, hors texte normatif.

Document : contrat ACT-L1 F04-B V0.3, transcrit tel que transmis par le fondateur le 12/11/2026 dans la session
d'implémentation, en réponse à la demande du texte intégral du freeze. Le fondateur l'identifie comme le contrat
« explicitement validé par le fondateur le 10 novembre 2026 » ; la décision de freeze limité (implémentation
dormante uniquement) a été transmise avec la demande d'implémentation du 11/11/2026.

Règles de transcription : texte reproduit sans reformulation, correction ni ajout, y compris la ligne de statut
interne (« CANDIDAT AU FREEZE — décision fondateur en attente ») et les coquilles éventuelles. Seul cet en-tête
commenté est ajouté. En cas de divergence avec toute autre documentation du dépôt (documentation d'implémentation,
matrice de traçabilité), le présent texte prévaut.

Documents associés : docs/act_l1_f04b_resources_v03.md (implémentation), docs/act_l1_f04b_traceability_v03.md
(exigences -> implémentation -> tests).
-->

# ACT-L1 — F04-B V0.3
## Contrat architectural de configuration et de ressources F2

**Statut : CANDIDAT AU FREEZE — décision fondateur en attente**

**Dépôt :** `robineaubasile-boop/oryx-api`  
**Base auditée :** `5ccc9e2bc122a44f1a2bfd810b28257bb6e31b31`  
**Référence normative :** `docs/act_l1_f04a_architecture_v03.md`

### 1. Objet et frontière

F04-B fournit les trois composants techniques nécessaires à la future composition F2 :

1. Un chargeur de configuration strict, sans effet externe.
2. Un gestionnaire de ressources PostgreSQL et fournisseurs, à propriété explicite.
3. Des précontrôles PostgreSQL exécutables en lecture seule.

F04-B ne compose aucune application ASGI, n'ajoute aucune route, ne démarre aucun serveur et ne modifie pas le point d'entrée public.

L'architecture existante demeure :

- F04-A : contrat architectural figé et fusionné.
- F04-B : infrastructure de configuration et ressources.
- F04-C : composition ASGI, lifespan et suivi des travaux.
- F04-D : confiance réseau et provenance HTTP, réserve P1.
- F04-E : qualification du drainage, des arrêts et des reprises, réserve P2.

**Le présent freeze, s'il est accepté, autorise uniquement l'implémentation dormante de F04-B. Il ne constitue pas une autorisation d'activation F2.**

### 2. B01 — Configuration et trousseaux

#### 2.1 Variables obligatoires

Le chargeur F04-B recevra explicitement un dictionnaire de configuration.

Noms contractuels proposés :

| Variable | Fonction |
|---|---|
| `DATABASE_URL` | PostgreSQL commun |
| `ORYX_F2_ALLOWED_ORIGIN` | Origine HTTPS explicite |
| `ORYX_F2_AUTH_KEYS_JSON` | Six usages du trousseau Auth F2 |
| `ORYX_F2_CSRF_KEYS_JSON` | Trousseau CSRF |
| `ORYX_F2_PORTFOLIO_KEYS_JSON` | Trousau Portefeuille |
| `ORYX_F2_DECRYPTAGE_KEYS_JSON` | Trousseau Décryptage |
| `ORYX_F2_SUPABASE_URL` | URL canonique Supabase |
| `ORYX_F2_SUPABASE_SERVICE_KEY` | Clé Supabase administrative |
| `ORYX_F2_SUPABASE_VERIFY_KEY` | Clé Supabase de vérification |
| `ORYX_F2_RESEND_API_KEY` | Clé Resend |
| `ORYX_F2_RESEND_SENDER` | Expéditeur OTP |
| `ORYX_F2_EODHD_API_KEY` | Clé EODHD |
| `ORYX_F2_FMP_API_KEY` | Clé FMP |
| `ORYX_F2_ANTHROPIC_API_KEY` | Clé Claude |
| `ORYX_F2_DECRYPTAGE_MODEL` | Modèle Claude C3 |

Aucun fournisseur n'est facultatif.

Aucun secret ne peut être généré automatiquement, remplacé par une valeur par défaut ou récupéré implicitement depuis une variable historique.

La liste des variables `ORYX_F2_*` acceptées est fermée. Les autres variables Railway ou legacy peuvent coexister sans être interprétées par le chargeur.

#### 2.2 Format contractuel des trousseaux

Quatre documents JSON versionnés.

Format Auth F2 :

```json
{
  "schema": "oryx-f2-auth-keys-v1",
  "keys": {
    "candidate": {"v1": "<base64url-43>"},
    "derive": {"v1": "<base64url-43>"},
    "active": {"v1": "<base64url-43>"},
    "otp": {"v1": "<base64url-43>"},
    "invitation": {"v1": "<base64url-43>"},
    "abuse": {"v1": "<base64url-43>"}
  },
  "current": {
    "candidate": "v1",
    "derive": "v1",
    "active": "v1",
    "otp": "v1",
    "invitation": "v1",
    "abuse": "v1"
  }
}
```

Pour CSRF, Portefeuille et Décryptage, structure commune :

```json
{
  "schema": "<identifiant-de-famille-v1>",
  "keys": {
    "v1": "<base64url-43>"
  },
  "current": "v1"
}
```

Identifiants exacts :

- `oryx-f2-csrf-keys-v1`
- `oryx-f2-portfolio-keys-v1`
- `oryx-f2-decryptage-keys-v1`

Chaque valeur doit représenter exactement 32 octets et être encodée en base64url canonique sans remplissage.

Chaque version doit respecter la syntaxe autorisée par sa classe existante ; les différences entre les quatre familles sont conservées.

**Refus obligatoires :** document absent, clé absente, JSON invalide, doublons de propriétés, propriété inconnue, mauvais type, mauvaise longueur, encodage non canonique, version courante manquante, réutilisation d'une même clé entre usages ou familles.

La comparaison de toutes les valeurs de clés est transversale aux quatre trousseaux.

Le même objet `Keyring` Auth F2 et le même objet `CsrfKeyring` devront être injectés aux deux futures compositions.

Le trousseau ne détermine pas à lui seul l'historique des rotations. La conservation et le retrait des anciennes versions nécessiteront une procédure distincte avant l'activation.

La version d'enregistrement anti-abus reste déterminée par PostgreSQL, non par `current["abuse"]`.

#### 2.3 Propriétés du chargeur

Le chargement doit :

- Être déterministe, sans connexion ni effet réseau.
- Produire des objets typés et immuables.
- Ne pas importer le moteur global legacy.
- Ne jamais journaliser une clé, un jeton ou une URL PostgreSQL contenant des identifiants.
- Refuser les configurations invalides avant toute construction de ressources.

Aucune lecture implicite de `os.environ` à l'import.

### 3. B02 — Dépendances de production

#### 3.1 Référence environnement

Python **3.11.9**, conforme à `runtime.txt`, aux journaux Railpack et à la CI.

Les versions actuellement qualifiées en CI constituent la base candidate :

| Bibliothèque | Version candidate |
|---|---|
| FastAPI | 0.110.0 |
| Uvicorn | 0.27.0 |
| Starlette | 0.36.3 |
| Anthropic | 1.11.0 |
| HTTPX | 0.28.1 |
| HTTPX2 | 2.13.1 |
| Requests | 2.34.2 |
| SQLAlchemy | 2.1.3 |
| psycopg2-binary | 2.9.13 |
| Alembic | 1.20.0 |

Les autres dépendances directes existantes doivent également conserver des versions qualifiées. Les dépendances transitives doivent être résolues et verrouillées de manière reproductible.

#### 3.2 Politique d'installation

`requirements.txt` reste le point d'entrée de l'installation Railway.

Une solution autorisée est un verrou de production distinct, référencé explicitement depuis `requirements.txt`, contenant des versions complètes et leurs empreintes. Le verrou CI peut conserver ses dépendances supplémentaires de test.

La sélection finale et le mécanisme de verrouillage doivent être démontrés par une installation propre Python 3.11.9.

L'absence de conflit ne suffit pas : les imports, les appels simulés des adaptateurs et les tests legacy/worker doivent réussir.

Aucun alias global `httpx` / `httpx2`, aucune suppression opportuniste des dépendances legacy, aucune modification de `runtime.txt`.

**Les versions listées sont des candidats autorisés pour qualification, et non des versions déclarées déjà vérifiées en production.**

### 4. B03 — Moteur PostgreSQL

Un seul moteur SQLAlchemy synchrone F2, construit explicitement puis partagé entre les futures compositions Auth et Personnel.

La fabrique n'importe ni `core.db.engine` ni `core.db.SessionLocal`.

La normalisation de `DATABASE_URL` doit rester compatible avec le comportement legacy : `postgres://` ou `postgresql://` vers `postgresql+psycopg2://`. Toute URL avec un driver non autorisé est refusée.

La normalisation ne doit pas altérer les mots de passe encodés.

**Isolation : `READ COMMITTED` explicite.**

Construction à l'appel explicite uniquement, jamais à l'import. L'ouverture effective d'une connexion est réservée au précontrôle ou à l'utilisation ultérieure.

Le moteur appartient au gestionnaire F04-B ; aucune route ne peut en devenir propriétaire.

### 5. B04 — Pool PostgreSQL

Paramètres candidats pour le pilote dormant :

| Paramètre | Valeur |
|---|---|
| `pool_size` | 5 |
| `max_overflow` | 2 |
| `pool_timeout` | 5 secondes |
| `pool_pre_ping` | `True` |
| `pool_recycle` | 1 800 secondes |
| `isolation_level` | `READ COMMITTED` |

Le plafond de ce pool F2 est de sept connexions empruntées simultanément par processus.

Ces valeurs sont **figées comme candidats à implémenter et à tester dans F04-B**, mais leur aptitude à l'exploitation réelle reste conditionnée à une qualification ultérieure.

La PR doit démontrer :

- Un pool effectivement borné.
- Une attente de connexion limitée.
- Aucun retry transactionnel automatique.
- Une isolation effective `READ COMMITTED`.
- Une fermeture possible après restitution des connexions.
- L'absence de second moteur F2 implicite.

**La capacité effective de PostgreSQL Railway, les connexions legacy/worker et les chevauchements de déploiement restent à qualifier avant l'activation.**

Une mesure défavorable entraînera une révision explicite des paramètres, et non un ajustement silencieux.

### 6. B05 — Ressources et transports fournisseurs

#### 6.1 Propriété générale

F04-B possède toutes les ressources créées pour sa composition : moteur PostgreSQL, clients HTTPX, clients SDK et sessions Requests.

Chaque ressource doit disposer d'un mode de fermeture identifiable.

En cas de construction partielle, les ressources acquises sont nettoyées en ordre inverse. Une fermeture échouée n'empêche pas la tentative de fermeture des autres ressources.

Une deuxième demande de fermeture doit être sans effet destructeur supplémentaire.

Une ressource injectée par un tiers ne doit pas être fermée comme si F04-B en était propriétaire. Les règles de propriété des transports injectés doivent être testées explicitement.

Aucune ressource n'est réputée libre sur la seule base de l'expiration d'un délai.

#### 6.2 Supabase et Resend

Conserver les méthodes métier existantes.

Autoriser une extension technique minimale de `core/auth_f2/http_providers.py` pour exposer la fermeture du client HTTPX possédé.

Préserver :

- TLS vérifié.
- `trust_env=False`.
- Aucune redirection suivie.
- Délais bornés.
- Classification des résultats et erreurs inchangée.
- Aucune donnée sensible journalisée.

Le partage des clients HTTPX est autorisé sous réserve des tests de concurrence et de fermeture.

#### 6.3 EODHD et FMP

Conserver les interfaces B2 et le mode historique de session injectée.

Ajouter un **mode de fabrique injectable de sessions** explicitement sélectionné par F04-B.

Dans ce mode de production, chaque appel fournisseur obtient une session Requests indépendante, laquelle est fermée à l'issue de l'appel, y compris sur erreur ou annulation observée à la frontière de l'appel.

Une session injectée directement reste sous la propriété de son appelant ; le comportement historique doit être conservé.

Le mode `session=` et le mode `session_factory=` ne doivent pas pouvoir être combinés de manière ambiguë.

Pour les sessions produites par F04-B, la politique réseau sera explicite : aucun proxy d'environnement implicite (`trust_env=False`), sauf évolution contractuelle ultérieure.

Interdictions :

- Modifier les méthodes B2.
- Ajouter des retries.
- Suivre des redirections.
- Modifier les seuils temporels.
- Modifier la classification des erreurs.
- Lire des variables de configuration depuis l'adaptateur.
- Partager une session mutable entre deux appels C3 concurrents.

Les tests existants avec sessions simulées doivent rester valides.

#### 6.4 Claude

Conserver `ClaudeTransport` et l'interface `create_message`.

Autoriser l'exposition d'une fermeture explicite du client SDK possédé.

Préserver `max_retries=0`, les délais, la structure des messages, les exceptions et le résultat normalisé.

La fermeture du client injecté, s'il existe, doit suivre une règle de propriété explicite et testée.

#### 6.5 Limite de responsabilité

F04-B fournit la fermeture technique.

Il ne démontre pas qu'une fermeture est sûre pendant qu'un fil C3 est actif.

Cette décision appartient au suivi des travaux F04-C et à la qualification P2 en F04-E.

### 7. B06 — Précontrôles PostgreSQL

Une opération explicite de précontrôle est fournie par F04-B.

Elle doit fonctionner sur une connexion PostgreSQL et dans une transaction effectivement en lecture seule.

Vérifications obligatoires :

1. Connexion au serveur prévu.
2. Isolation effective `READ COMMITTED`.
3. Version Alembic en base compatible avec la tête attendue du code déployé.
4. Présence des objets nécessaires à la composition F2.
5. État `auth_abuse_key_state` présent et compatible avec les versions du trousseau, au moyen de `load_key_state`.
6. Absence d'incohérence détectable dans les préconditions essentielles.

La référence de migration est dérivée d'une source versionnée avec le code, sans importer `alembic/env.py` ni exécuter de migrations.

L'état absent ou incompatible entraîne un échec fermé.

Interdits :

- `alembic upgrade`.
- `initialize_abuse_key_state`.
- Création de tables ou de lignes.
- Rotation ou purge de clés.
- Réparation silencieuse du schéma.
- Écriture de marqueurs anti-abus.

Les données nécessaires à une future activation doivent être préparées par une opération d'administration distincte.

### 8. Contrat de remise à F04-C

F04-B expose trois capacités séparées :

**A. Charger la configuration** : opération pure et déterministe.

**B. Construire les ressources** : opération explicite avec registre de propriété et nettoyage des constructions partielles.

**C. Vérifier les préconditions** : opération PostgreSQL en lecture seule, avec résultat de réussite ou échec fermé.

F04-C est responsable de les appeler dans le bon ordre, de posséder le lifespan et de suivre les travaux.

F04-B ne définit pas l'admission HTTP, le résolveur d'IP, le routage ni les conditions opérationnelles de drainage.

### 9. Périmètre autorisé de la PR

**Autorisés :**

- Nouveaux modules de configuration et de gestion des ressources F04-B.
- Nouveaux tests dédiés.
- Modification de `requirements.txt` et création ou régénération justifiée des verrous de dépendances.
- Adaptations techniques limitées à la fermeture dans `core/auth_f2/http_providers.py`.
- Adaptations techniques limitées aux sessions et à la fermeture dans `core/decryptage_providers.py`.
- Ajustements strictement nécessaires des tests existants pour accompagner ces évolutions, sans supprimer leurs garanties.
- Documentation technique du lot F04-B.

**Interdits :**

- Modifier `api.py` ou le point d'entrée Railway.
- Modifier `core/db.py` ou utiliser son moteur global pour F2.
- Modifier les runners transactionnels, les commandes, B1, B2 ou C3.
- Modifier les contrats de refus ou les routes HTTP.
- Modifier le gate F-02A.
- Ajouter une migration ou initialiser des données de production.
- Changer Railway, créer des variables ou afficher des secrets.
- Ajouter un mécanisme d'activation, de bypass ou de fallback vers le legacy.

Toute évolution touchant une frontière non autorisée entraîne une nouvelle décision architecturale avant implémentation.

### 10. Critères de GO / NO-GO de la PR

Le merge est refusé si l'une des preuves suivantes manque :

| Domaine | Preuve obligatoire |
|---|---|
| B01 | Configuration invalide refusée, doublons et collisions de clés détectés, secrets non divulgués |
| B02 | Installation de production seule réussie avec Python 3.11.9, dépendances importables, cohérence avec la CI |
| B03 | Un seul moteur F2, aucune dépendance au moteur global legacy, isolation effective |
| B04 | Pool borné, saturation testée sur base jetable, attente limitée |
| B05 | Concurrence sans session Requests partagée, fermeture dans les cas nominaux et d'échec |
| B05 | Contrats Supabase, Resend, EODHD, FMP et Claude préservés |
| B06 | Précontrôles en lecture seule, état anti-abus absent/incompatible refusé |
| Dormance | Aucun changement du comportement des points d'entrée publics |
| Régression | Suites PostgreSQL 16/18, migrations et web réussies |
| Railway | API et worker opérationnels après merge, si déployés |

Les tests doivent utiliser des secrets synthétiques, des fournisseurs simulés et des bases jetables.

La réussite de ces tests autorise un audit de merge, **pas une activation F2**.

### 11. Réserves après F04-B

Même avec une PR F04-B intégralement conforme, les points suivants resteront ouverts :

- **P1 :** preuve de confiance dans la provenance IP Railway.
- **P2 :** maintenance, drainage, arrêt forcé, COMMIT incertain et reprise.
- Qualification du pool sur la capacité SQL réelle de production.
- Préparation contrôlée des secrets et de l'état anti-abus.
- F04-C, F04-D, F04-E, F-05 et basculement F-03.
- Décision d'activation explicite du fondateur.

Aucun des résultats de F04-B n'autorise un retour automatique vers `api.py` public après retrait des routes legacy.

### 12. Décision de freeze proposée

**FREEZE LIMITÉ F04-B V0.3 — IMPLÉMENTATION DORMANTE UNIQUEMENT**

Ce freeze porterait sur :

- Les contrats B01 à B06 exposés ci-dessus.
- Les paramètres candidats de la première implémentation.
- La liste nominative des fichiers et composants modifiables.
- Les tests et preuves obligatoires avant merge.
- Le maintien des réserves P1 et P2.

Il n'autoriserait ni l'intégration publique de F2, ni une modification Railway, ni un merge automatique.

**Statut actuel : en attente de décision explicite du fondateur.**
