"""
Bibliothèque pédagogique Oryx Invest.

Contient les méthodes canoniques Oryx pour les sujets d'analyse
fondamentale. Consultée par la route /pedagogie/lookup avant l'appel
au LLM Claude pour injecter la méthode dans le system prompt.

Pipeline de détection :
1. Normalisation de la question (lowercase + suppression accents)
2. Détection des sous-méthodes (analyse_qualitative_*) en priorité
3. Détection des méthodes principales (bilan_financier, ratios_valorisation,
   analyse_qualitative)
4. Si aucun match : retourne None (Claude répond librement)
"""

import unicodedata
from typing import Optional


def _normalize(text: str) -> str:
    """Normalise une chaîne pour matching keyword insensible à la casse et aux accents."""
    if not text:
        return ""
    # Décomposition Unicode + suppression des diacritiques
    nfd = unicodedata.normalize("NFD", text)
    without_accents = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return without_accents.lower()


# Bibliothèque de méthodes pédagogiques canoniques Oryx
METHODES = {

    "bilan_financier": {
        "title": "Lire les états financiers",
        "keywords": [
            "bilan", "etats financiers", "compte de resultat", "p&l",
            "cash flow", "tableau des flux", "tableau de flux",
            "balance sheet", "bilan comptable",
            "lire un bilan", "analyser une entreprise", "comprendre les comptes",
            "income statement", "cash flow statement"
        ],
        "example_company": "Netflix 2023 + LVMH 2023",
        "method_content": """LECTURE DES ÉTATS FINANCIERS — MÉTHODE ORYX

Analyser une entreprise, c'est lire 3 documents complémentaires.
Chacun répond à une question précise. Ensemble, ils racontent
toute l'histoire.

LES 3 ÉTATS FINANCIERS

Document            Question clé                    Ce qu'il montre
P&L                 Comment génère-t-elle de la     Revenus → Bénéfice
                    valeur ?                        comptable
Cash Flow Statement Où va le vrai cash ?            Cash qui entre
                                                    et sort réellement
Balance Sheet       Que possède-t-elle et que       Actifs vs Dettes
                    doit-elle ?                     vs Capitaux propres

NIVEAU 1 — LE COMPTE DE RÉSULTAT (P&L)

Se lit de haut en bas : on part du chiffre d'affaires, on soustrait
les coûts jusqu'au bénéfice final.

Structure : Revenue → COGS → Gross Profit → OpEx → EBIT → Net Income

Indicateurs clés :
- Gross Margin (%) = Gross Profit / Revenue → RÉVÈLE LE MOAT
  Luxe : ~69% (LVMH) | Industrie : 20-30% | SaaS : 70-80%
- EBIT = performance opérationnelle pure (avant financement et fisc)
- Net Income = ce qui reste après tout (mais ≠ cash réel)

INSIGHT FONDAMENTAL : Net Income ≠ Cash réel. Toujours aller lire
le Cash Flow Statement pour vérifier.

NIVEAU 2 — LE TABLEAU DES FLUX (Cash Flow Statement)

Part du Net Income et le réconcilie avec le cash réellement généré.

Structure clé :
Net Income
+ D&A (charge non-cash, on la rajoute)
+/- Variation du BFR (cash coincé dans le cycle opérationnel)
= CFO (Cash From Operations) — le vrai pouls de l'entreprise
- CAPEX (investissements en actifs physiques)
= FCF (Free Cash Flow) — cash disponible pour les actionnaires

LE FCF EST LE CHIFFRE LE PLUS IMPORTANT. C'est le cash réellement
encaissé après tous les investissements nécessaires pour maintenir
l'activité. Difficile à manipuler comptablement.

Payout Ratio = Dividendes / FCF. Sous 60% : sain. Au-dessus de 80% :
signal d'alarme, l'entreprise ne réinvestit plus assez.

NIVEAU 3 — LE BILAN (Balance Sheet)

Photo à un instant T. Deux colonnes toujours égales : Actif = Passif.

Actif : ce qu'elle possède (Cash, Stocks, Créances, PP&E, Goodwill)
Passif : comment c'est financé (Dettes CT et LT + Capitaux propres)

3 RATIOS CLÉS DU BILAN :
- Dette Nette / EBITDA : solvabilité (< 3x sain, > 5x dangereux)
- Current Ratio = Actifs CT / Dettes CT : liquidité (> 1x sain)
- ROE = Net Income / Equity : rentabilité (> 15% bon, > 20% excellent)

APPLICATION NETFLIX 2023 (en Md$)

ACTIF : Cash 7 | Catalogue contenus 32 | Autres 9 → Total ~48
PASSIF : Dette LT 14 | Autres dettes 14 | Capitaux propres 20 → ~48

Histoire racontée : Netflix a investi 32 Md$ dans son catalogue,
financé en partie par 14 Md$ de dette. La valeur de l'entreprise
dépend de sa capacité à amortir et renouveler ce catalogue.

LES 3 QUESTIONS À TE POSER SUR N'IMPORTE QUEL BILAN

1. L'entreprise a-t-elle plus de dettes que de capitaux propres ?
2. A-t-elle assez de cash pour tenir si ça tourne mal ?
3. Où est concentré l'actif ? (révèle le modèle économique)

LA RÈGLE À RETENIR

Taille → concentration de l'actif → structure de financement →
liquidité. Toujours dans cet ordre. Le reste, c'est du détail
d'analyste.

Les 3 niveaux sont COMPLÉMENTAIRES, pas alternatifs. Un investisseur
sérieux les lit dans l'ordre P&L → CFS → Bilan car la performance
explique le cash qui modifie le patrimoine."""
    },

    "ratios_valorisation": {
        "title": "Les ratios de valorisation",
        "keywords": [
            "valorisation", "valoriser", "valoriser une action",
            "ratio de valorisation", "ratios de valorisation",
            "p/e", "per", "price earning", "price-to-earnings",
            "ev/ebitda", "enterprise value",
            "p/b", "price-to-book", "price to book",
            "p/fcf", "price-to-free cash flow",
            "action chere", "action pas chere",
            "a quel prix acheter", "prix d'une action", "juger le prix",
            "multiple de valorisation"
        ],
        "example_company": "LVMH 2023",
        "method_content": """LES RATIOS DE VALORISATION — MÉTHODE ORYX

Une fois qu'on sait lire les états financiers, la question suivante
est : à quel prix achète-t-on ? Les ratios de valorisation comparent
le prix de marché à la réalité financière de l'entreprise. Ils
répondent à la question : "l'action est-elle chère ou bon marché
par rapport à ce qu'elle génère ?"

LES 4 RATIOS À MAÎTRISER

Ratio       Source              Basé sur        Question posée
P/E         Compte de résultat  Net Income      Combien on paie pour
                                                1€ de bénéfice net ?
EV/EBITDA   P&L + Bilan         EBITDA          Valorisation globale
                                                sans biais fiscal ?
P/B         Bilan               Capitaux propres Paie-t-on plus que
                                                la valeur comptable ?
P/FCF       Cash Flow Statement Free Cash Flow  Combien on paie pour
                                                1€ de vrai cash ?

1. LE P/E — Price-to-Earnings

Formule : P/E = Prix de l'action / Bénéfice par action (EPS)

Seuils de référence :
- < 10x : potentiellement sous-évalué ou secteur en difficulté
- 10-20x : zone "raisonnable" (dépend du secteur)
- 20-30x : croissance attendue intégrée dans le prix
- > 30x : forte prime de croissance, risque si déception

LIMITE CRITIQUE : Le Net Income peut être manipulé comptablement
(charges exceptionnelles, différés fiscaux). Un P/E bas peut cacher
une entreprise en déclin. Un P/E élevé peut être justifié si la
croissance est forte. TOUJOURS comparer au secteur et aux pairs.

2. L'EV/EBITDA — Enterprise Value / EBITDA

Formule :
EV = Capitalisation boursière + Dette nette
EV/EBITDA = EV / EBITDA

C'est le ratio privilégié des analystes professionnels car il est
INDÉPENDANT DE LA STRUCTURE DE FINANCEMENT ET DE LA FISCALITÉ.
Permet de comparer des entreprises de pays différents.

Seuils : < 6x basse | 6-12x normale | 12-20x qualité | > 20x premium

INSIGHT : L'EV/EBITDA inclut la dette. Deux entreprises avec la
même capitalisation boursière peuvent avoir des EV très différentes
si l'une est endettée.

3. LE P/B — Price-to-Book

Formule : P/B = Prix de l'action / Valeur comptable par action

Compare le prix de marché aux capitaux propres comptables. Répond
à : "paie-t-on plus que ce que l'entreprise vaut sur le papier ?"

Seuils : < 1x value/difficulté | 1-3x normale | 3-10x forte rentabilité
| > 10x intangibles non comptabilisés (marques, IP)

LE CAS DES INTANGIBLES : Un P/B élevé n'est pas nécessairement cher.
Louis Vuitton ou Google n'apparaissent pas à leur vraie valeur au
bilan. Le P/B est peu utile seul — IL FAUT LE CROISER AVEC LE ROE.

Règle d'or : P/B élevé + ROE élevé = création de valeur justifiée
             P/B élevé + ROE faible = danger de surpaiement

4. LE P/FCF — Price-to-Free Cash Flow

Formule : P/FCF = Capitalisation boursière / Free Cash Flow

LE RATIO LE PLUS HONNÊTE car basé sur le FCF — le cash réellement
disponible après investissements. Beaucoup plus difficile à manipuler
comptablement que le Net Income.

Seuils : < 10x sous-évalué | 10-20x attractive | 20-30x normale | > 30x premium

P/FCF vs P/E — Pourquoi préférer le FCF :
- P/E basé sur Net Income → manipulable comptablement (D&A, charges
  non-cash, ajustements fiscaux)
- P/FCF basé sur Free Cash Flow → cash réellement encaissé après
  CAPEX → DIFFICILE À FALSIFIER

INSIGHT FONDAMENTAL : Si le P/E est bas mais le P/FCF est élevé,
c'est un SIGNAL D'ALARME : les bénéfices comptables ne se
transforment pas en vrai cash.

APPLICATION LVMH 2023

Prix action : ~750 € | Capitalisation : ~375 Md€

Données utilisées : Revenue 86 Md€ | EBITDA ~29 Md€ | Net Income
15 Md€ | FCF 15 Md€ | Capitaux propres ~56 Md€

Calculs :
- P/E = 375 / 15 = ~25x → moyenne du luxe, reflète qualité du moat
- EV/EBITDA = 390 / 29 = ~13x → raisonnable pour un leader luxe
- P/B = 375 / 56 = ~6,7x → justifié par ROE 21,8% + valeur marques
- P/FCF = 375 / 15 = ~25x → cohérent avec P/E, bénéfices réels

LECTURE : LVMH affiche des ratios cohérents et en ligne avec un
business de haute qualité. P/FCF confirme P/E → bénéfices réels.
EV/EBITDA raisonnable pour un monopole du luxe. P/B élevé justifié
par un ROE exceptionnel.

LES 5 QUESTIONS D'UN INVESTISSEUR (VALORISATION)

1. L'action est-elle chère vs ses bénéfices ? → P/E vs moyenne sectorielle
2. La valeur totale est-elle justifiée ? → EV/EBITDA < 15x pour mature
3. Paie-t-on plus que la valeur comptable ? → P/B croisé avec ROE
4. Le prix reflète-t-il le vrai cash ? → P/FCF (ratio le plus fiable)
5. La valorisation est-elle cohérente ? → Convergence des 4 ratios = signal fort

LA RÈGLE À RETENIR

Aucun ratio ne se suffit à lui-même. La force d'une analyse de
valorisation vient de la CONVERGENCE des signaux. Quand P/E,
EV/EBITDA, P/B (croisé ROE) et P/FCF racontent la même histoire,
le diagnostic est fiable. Quand ils divergent — surtout P/E vs
P/FCF — il faut creuser.

Tout ratio doit être interprété dans son contexte sectoriel et
comparé aux pairs historiques."""
    },

    "analyse_qualitative": {
        "title": "L'analyse qualitative — vue d'ensemble",
        "keywords": [
            "analyse qualitative", "qualitatif", "qualitative",
            "au-dela des chiffres", "au dela des chiffres",
            "ce que les chiffres ne disent pas",
            "approche qualitative", "fondamentaux qualitatifs"
        ],
        "example_company": "LVMH 2023",
        "method_content": """L'ANALYSE QUALITATIVE — MÉTHODE ORYX

Les états financiers disent CE QU'UNE ENTREPRISE A PRODUIT DANS
LE PASSÉ. L'analyse qualitative tente de répondre à une question
différente : "pourquoi cette entreprise continuera-t-elle à gagner
de l'argent dans 10 ans ?"

C'est cette dimension — invisible dans les chiffres — qui distingue
un bon investisseur d'un simple lecteur de bilans.

LES 4 PILIERS DE L'ANALYSE QUALITATIVE

Pilier              Question centrale               Ce qu'on cherche
1. Moat             Pourquoi les clients            Avantage compétitif
                    reviennent-ils ?                durable
2. Management       Qui pilote le business —        Alignement, track
                    et comment ?                    record, capital
                                                    allocation
3. Secteur          Dans quel environnement         Croissance,
                    l'entreprise évolue-t-elle ?    concurrence,
                                                    régulation
4. Avantages        Qu'est-ce que personne          Barrières à
   compétitifs      d'autre ne peut copier ?        l'entrée concrètes

POURQUOI CES 4 PILIERS DANS CET ORDRE

1. Le MOAT répond au "pourquoi" stratégique : ce qui protège
   l'entreprise contre la concurrence.
2. Le MANAGEMENT répond au "qui" : ceux qui transforment ce
   moat en valeur (ou le détruisent).
3. Le SECTEUR répond au "où" : l'environnement structurel détermine
   le plafond de rentabilité atteignable.
4. Les AVANTAGES COMPÉTITIFS répondent au "comment concrètement" :
   les preuves tangibles que le moat existe.

APPLICATION LVMH 2023 — VUE D'ENSEMBLE

Pilier          Évaluation LVMH
Moat            Exceptionnel — 75 maisons de luxe avec 100+ ans
                d'histoire chacune. Impossible à répliquer.
Management      Fort alignement — famille Arnault 47% capital.
                Track record de 40 ans de création de valeur.
Secteur         Très favorable — oligopole mondial (LVMH, Hermès,
                Richemont). Barrières culturelles + croissance Asie.
Avantages       Imbattables — réseau de 6000+ boutiques propres.
                Héritage artisanal = différenciation permanente.

Risque qualitatif réel : dépendance au consommateur aisé asiatique
(35-40% du CA) + vision d'un seul homme (Bernard Arnault).

LA RÈGLE À RETENIR

L'analyse qualitative est SUBJECTIVE par nature. Les 4 piliers
sont des outils de structuration de la réflexion, pas des formules
mécaniques. Un bon investisseur évalue chaque pilier séparément,
puis cherche la convergence : les meilleures entreprises
qualitatives sont fortes sur les 4 dimensions simultanément.

POUR APPROFONDIR

Chaque pilier mérite une analyse dédiée :
- "Explique-moi le moat" → analyse du Moat en détail
- "Comment évaluer le management ?" → 4 dimensions du management
- "Analyser le secteur" → 5 forces de Porter
- "Les avantages compétitifs" → checklist concrète des barrières"""
    },

    "analyse_qualitative_moat": {
        "title": "Le Moat — l'avantage compétitif durable",
        "keywords": [
            "moat", "douve", "douve economique",
            "avantage competitif durable",
            "barriere concurrentielle",
            "buffett", "protection contre la concurrence",
            "effet reseau", "couts de switching", "switching costs"
        ],
        "example_company": "LVMH (marques) + Visa (réseau) + ASML (brevets)",
        "method_content": """LE MOAT — L'AVANTAGE COMPÉTITIF DURABLE

Le concept de moat (fossé en anglais) vient de Warren Buffett. Il
désigne la capacité d'une entreprise à DÉFENDRE SES MARGES ET SA
POSITION FACE À LA CONCURRENCE DANS LA DURÉE.

Sans moat, les profits s'érodent mécaniquement : un concurrent
copie, baisse les prix, et la rente disparaît. Avec moat,
l'entreprise conserve son pricing power et sa rentabilité même
sous pression concurrentielle.

LES 5 SOURCES DE MOAT

Type de moat        Mécanisme                       Exemples
Effet réseau        Le produit devient plus utile   Visa, LinkedIn,
                    à mesure que les utilisateurs   Airbnb
                    augmentent
Coûts de switching  Changer de fournisseur est      SAP, Salesforce,
                    coûteux ou douloureux pour      Bloomberg Terminal
                    le client
Actifs intangibles  Marques, brevets, licences      LVMH, Hermès,
                    réglementaires impossibles      ASML
                    à répliquer
Avantage de coût    Structure de coûts durablement  Costco, Ryanair
                    inférieure à celle des
                    concurrents
Échelle efficiente  Marché de niche où un seul      Aéroports
                    acteur est rentable — le        régionaux,
                    deuxième ne peut pas            pipelines

COMMENT DÉTECTER UN MOAT DANS LES CHIFFRES

Signal financier             Ce qu'il révèle           Seuil indicatif
Gross Margin élevée stable   Pricing power — client    > 50% = fort moat
                             paie sans négocier
ROIC > coût du capital       Entreprise crée de la     ROIC > 15% solide
(WACC)                       valeur, pas juste du CA
FCF margin croissante        Rentabilité s'améliore    > 15% FCF/CA
                             avec la taille
Faible turnover clients      Le client reste —         Churn < 5% par an
                             rétention = preuve du moat

INSIGHT CRITIQUE : Un moat n'est PAS PERMANENT. Il se dégrade si
la technologie change (Kodak), si la réglementation évolue, ou
si un concurrent trouve une façon de le contourner.

L'investisseur doit se demander : CE MOAT SERA-T-IL INTACT DANS
10 ANS ?

PIÈGES À ÉVITER

- Confondre TAILLE et MOAT : une grande entreprise n'a pas
  forcément d'avantage durable. Vérifier les marges sur 10 ans.
- Croire au "first-mover advantage" : être le premier ne garantit
  pas de rester leader. Chercher les vrais switching costs.
- Surévaluer le moat technologique : la tech évolue vite, un
  avantage peut disparaître en 3 ans. Préférer les avantages
  structurels (marque, réseau, échelle).

LA RÈGLE À RETENIR

Pas de moat = pas d'investissement long terme. Un moat solide est
la condition nécessaire (mais pas suffisante) pour qu'une
entreprise crée de la valeur durablement. C'est la fondation de
tout investissement de qualité."""
    },

    "analyse_qualitative_management": {
        "title": "Le Management — qui pilote et comment",
        "keywords": [
            "management", "dirigeant", "dirigeants", "ceo",
            "capital allocation", "allocation du capital",
            "qualite du management",
            "evaluer le management",
            "track record", "alignement interets",
            "skin in the game", "insider ownership",
            "famille fondatrice", "actionnaire fondateur"
        ],
        "example_company": "Famille Arnault (LVMH) + Mark Zuckerberg (Meta)",
        "method_content": """LE MANAGEMENT — QUI PILOTE ET COMMENT

Un bon business avec un mauvais management peut être détruit. Un
business médiocre avec un excellent management peut être transformé.

Le management est l'une des variables LES PLUS SOUS-ESTIMÉES par
les investisseurs particuliers — et l'une des plus scrutées par
les institutionnels.

LES 4 DIMENSIONS À ÉVALUER

Dimension            Question à poser              Signaux
Track record         A-t-il déjà créé de la        ✓ ROE croissant sur 5 ans
                     valeur dans un rôle           ✗ Promesses non tenues,
                     similaire ?                     guidance manquée
Alignement           Possède-t-il des actions      ✓ Insider ownership > 5%
d'intérêts           de l'entreprise ?             ✗ Stock-options massives
                                                     sans performance
Capital allocation   Réinvestit-il bien le         ✓ ROIC > 15% sur
                     cash généré ?                   acquisitions
                                                   ✗ Acquisitions surévaluées,
                                                     goodwill excessif
Communication        Est-il transparent sur        ✓ Admet les erreurs,
                     les difficultés ?               chiffres cohérents
                                                   ✗ Euphémismes, métriques
                                                     changeantes

LA CAPITAL ALLOCATION — LE TEST ULTIME

Ce que le management fait avec le cash excédentaire est le VRAI
RÉVÉLATEUR DE SA QUALITÉ. Il a 5 options :

Usage du cash           Bon signal si...           Mauvais signal si...
Réinvestissement        ROIC > 15%, marché en      Rendements décroissants,
organique               croissance                 surcapacité
Acquisitions            Prix raisonnable,          Surpaiement, goodwill
                        synergies réelles          > 30% des actifs
Rachat d'actions        Action sous-valorisée      Action chère, dette
                        (P/FCF < 15x)              financée par rachat
Remboursement dette     Bilan fragile,             Pas de dette mais
                        environnement risqué       remboursement défensif
Dividendes              FCF stable et prévisible   Dividende > FCF =
                                                   insoutenable

INSIGHT CRITIQUE : Méfie-toi des dirigeants qui changent les
métriques de performance à chaque rapport annuel. Quand un CEO
modifie sa définition de "EBITDA ajusté" trois années de suite,
c'est souvent pour cacher une dégradation.

LES SIGNAUX POSITIFS RARES MAIS PRÉCIEUX

- Insider ownership élevé (> 5% du capital) : skin in the game réel
- Lettres annuelles aux actionnaires détaillées et franches (Buffett,
  Bezos, Dimon) : transparence et vision long terme
- Reconnaissance publique d'erreurs passées : maturité managériale
- Cohérence entre déclarations et actions sur 5+ ans : crédibilité

LES SIGNAUX D'ALARME

- Stock-options massives non liées à la performance
- Rémunération basée sur le BPA ajusté (manipulable) plutôt que
  sur le ROIC
- Acquisitions multiples sans intégration claire
- Turnover élevé dans le top management (CFO qui partent)

LA RÈGLE À RETENIR

Le management est invisible dans les chiffres mais visible dans
LEUR ÉVOLUTION. Un grand dirigeant laisse une trace mesurable :
ROIC qui s'améliore, marges qui se stabilisent, dette qui se
réduit. Si tu veux juger un management, regarde les 5 dernières
années d'allocations de capital, pas les présentations PowerPoint."""
    },

    "analyse_qualitative_secteur": {
        "title": "Le Secteur — l'environnement qui détermine le plafond",
        "keywords": [
            "secteur", "industrie", "marche",
            "porter", "5 forces", "cinq forces", "forces de porter",
            "analyse sectorielle", "environnement concurrentiel",
            "structure du marche",
            "barrieres a l'entree",
            "pouvoir des fournisseurs", "pouvoir des clients",
            "rivalite concurrentielle"
        ],
        "example_company": "Luxe (oligopole) vs Aérien (fragmenté)",
        "method_content": """LE SECTEUR — L'ENVIRONNEMENT QUI DÉTERMINE LE PLAFOND

Même le meilleur management ne peut pas durablement surperformer
dans un secteur structurellement défavorable. Comprendre la
dynamique sectorielle, c'est comprendre LE PLAFOND DE CROISSANCE
ET DE RENTABILITÉ atteignable par l'entreprise.

LES 5 FORCES DE PORTER — OUTIL FONDAMENTAL

Force                      Question                      Impact
Rivalité                   Combien de concurrents ?      Fort = marges
concurrentielle            Guerre des prix ?             compressées
Pouvoir des                Peuvent-ils augmenter         Fort = coûts
fournisseurs               leurs prix librement ?        incontrôlables
Pouvoir des                Les clients peuvent-ils       Fort = pricing
clients                    négocier ou partir ?          power limité
Nouveaux entrants          Facile de créer un            Facile = moat
                           concurrent ?                  fragile
Produits de substitution   Existe-t-il une alternative   Oui = plafond
                           qui remplace le besoin ?      sur les prix

SECTEURS ATTRACTIFS VS DIFFICILES

Secteur attractif                          Secteur difficile
Barrières à l'entrée élevées (régulation,  Commodités — prix fixé par
capital, marques)                          le marché mondial
Demande prévisible et récurrente           Forte cyclicité — aérien,
(logiciels, santé)                         acier, shipping
Consolidation — peu d'acteurs              Fragmentation — milliers de
dominants                                  concurrents locaux
Croissance du marché tirée par             Marché mature ou en déclin
des tendances de fond                      structurel

INSIGHT CRITIQUE : Un secteur en croissance de 20% par an peut
rendre médiocre un investissement si la concurrence est féroce
et les marges inexistantes. LA CROISSANCE DU SECTEUR ≠
RENTABILITÉ POUR LES ACTEURS.

Exemple typique : le marché des panneaux solaires a explosé en
volume, mais la plupart des fabricants chinois perdent de l'argent
à cause de la guerre des prix.

COMMENT ÉVALUER LA SANTÉ STRUCTURELLE D'UN SECTEUR

1. Regarder les marges opérationnelles moyennes des leaders sur
   10 ans. Stables ou en hausse = secteur sain. Volatiles ou en
   baisse = pression structurelle.

2. Identifier le "price-setter" : qui fixe les prix dans la chaîne
   de valeur ? Si c'est un fournisseur ou un client, l'entreprise
   est coincée. Si c'est elle-même, elle a du pricing power.

3. Vérifier si les barrières à l'entrée AUGMENTENT ou DIMINUENT
   avec le temps. La régulation croissante (banques, pharma) ou
   l'effet de réseau qui se renforce (plateformes) sont des bons
   signes.

4. Évaluer la cyclicité : un secteur cyclique exige une valorisation
   plus prudente (multiples plus bas), même pour le leader.

SECTEURS HISTORIQUEMENT RENTABLES POUR LES INVESTISSEURS

- Luxe (oligopole mondial, barrières culturelles)
- Logiciels SaaS (switching costs élevés, marges récurrentes)
- Santé/Pharma (régulation, brevets)
- Tabac (oligopole, demande inélastique — éthique à part)
- Paiements (effet réseau, scalabilité)

SECTEURS HISTORIQUEMENT DIFFICILES

- Aérien (cyclicité + concurrence sur les prix)
- Sidérurgie/Matières premières (commodités)
- Distribution traditionnelle (fragmentation + Amazon)
- Restauration (faibles barrières + sensibilité aux coûts)

LA RÈGLE À RETENIR

Le secteur détermine 50% de la performance possible. Une
entreprise médiocre dans un excellent secteur fera mieux qu'une
excellente entreprise dans un secteur structurellement défavorable.
Toujours analyser le terrain de jeu avant l'équipe."""
    },

    "analyse_qualitative_avantages_competitifs": {
        "title": "Les avantages compétitifs concrets",
        "keywords": [
            "avantage competitif",
            "avantages competitifs",
            "barrieres a l'entree",
            "ce que personne ne peut copier",
            "reseau de distribution",
            "brevets", "donnees exclusives",
            "contrats long terme", "economies d'echelle",
            "licence reglementaire",
            "marque premium"
        ],
        "example_company": "LVMH (marques) + ASML (brevets) + Coca-Cola (distribution)",
        "method_content": """LES AVANTAGES COMPÉTITIFS CONCRETS

Au-delà du moat conceptuel, l'investisseur doit identifier les
BARRIÈRES À L'ENTRÉE CONCRÈTES qui protègent l'entreprise. Ce sont
les actifs et positions que les concurrents ne peuvent pas
répliquer facilement, même avec des milliards.

Le moat est le concept. Les avantages compétitifs sont les preuves
tangibles de son existence.

CHECKLIST DES AVANTAGES COMPÉTITIFS

Avantage              Comment le vérifier              Exemple concret
Marque premium        Gross margin >> moyenne          LVMH (69%),
                      sectorielle                      Apple (43%)
Brevets / IP          Durée, nombre, part du CA        ASML (EUV
                      protégée                         lithographie),
                                                       Novo Nordisk
Données exclusives    Taille et unicité de la base     Bloomberg,
                      de données                       Moody's, MSCI
Réseau de             Coût et délai pour un entrant    Coca-Cola,
distribution          de le répliquer                  Nestlé
Contrats long terme   Durée résiduelle, taux de        Airbus (backlog
                      renouvellement                   8 ans), ASML
Licence               Impossible à obtenir sans        Bourses, banques,
réglementaire         historique                       pharmacies
Économies d'échelle   Coût unitaire décroissant        Amazon AWS, TSMC
                      avec la taille

CES 7 AVANTAGES NE SONT PAS ÉGAUX

Les avantages STRUCTURELS (régulation, échelle, distribution
physique) sont plus durables que les avantages CIRCONSTANCIELS
(marque tendance, technologie de pointe).

Une marque comme Hermès, construite sur 200 ans d'artisanat, est
plus durable qu'une marque de mode rapide. Un brevet pharma de 20
ans est plus durable qu'un brevet logiciel contournable en 3 ans.

LES 5 QUESTIONS D'UN INVESTISSEUR

Question                          Pilier         Indicateur clé
L'entreprise a-t-elle un          Moat           Gross Margin > moyenne
avantage durable ?                               sectorielle + stable
Le management crée-t-il           Management     ROIC > 15% sur 5 ans,
de la valeur ?                                   alignement intérêts
Le secteur permet-il de           Secteur        Forces de Porter
gagner de l'argent ?                             favorables, croissance
                                                 visible
Y a-t-il des barrières à          Avantages      Marques, brevets,
l'entrée réelles ?                               réseau, régulation
Les qualités qualitatives se      Synthèse       FCF croissant,
reflètent dans les chiffres ?                    ROE > 15%, marges stables

PIÈGES À ÉVITER

Piège                          Description                    Comment l'éviter
Confondre taille et moat       Une grande entreprise n'a      Vérifier les marges
                               pas forcément d'avantage       sur 10 ans
                               durable
Moat technologique fragile     La tech évolue vite — un       Préférer les
                               avantage peut disparaître      avantages
                               en 3 ans                       structurels
Le management comme moat       Un homme clé n'est pas un      Que se passe-t-il
                               avantage systémique            si il part ?
Le first-mover advantage       Être le premier ne garantit    Chercher les vrais
                               pas de rester leader           switching costs

INSIGHT FONDAMENTAL : Les avantages qualitatifs DOIVENT se
refléter dans les chiffres. Si une entreprise prétend avoir une
marque premium mais que sa Gross Margin est dans la moyenne
sectorielle, c'est que la marque n'a pas de pricing power réel.

LE TEST DE COHÉRENCE : qualité qualitative + chiffres médiocres =
illusion. Qualité qualitative + chiffres exceptionnels = vraie
qualité.

LA RÈGLE À RETENIR

Demande-toi pour chaque investissement : "qu'est-ce que cette
entreprise possède que personne ne peut copier en 5 ans, même
avec 10 milliards ?" Si la réponse n'est pas claire et concrète,
il n'y a pas d'avantage compétitif réel — juste de la marge
conjoncturelle qui finira par s'éroder."""
    },

}


# Sous-méthodes prioritaires (détectées avant les méthodes principales)
# Ordre IMPORTANT : du plus spécifique au plus général
SOUS_METHODES_PRIORITAIRES = [
    "analyse_qualitative_moat",
    "analyse_qualitative_management",
    "analyse_qualitative_secteur",
    "analyse_qualitative_avantages_competitifs",
]

# Méthodes principales (détectées si aucune sous-méthode ne matche)
METHODES_PRINCIPALES = [
    "bilan_financier",
    "ratios_valorisation",
    "analyse_qualitative",
]


def lookup_method(question: str, context: Optional[str] = None) -> Optional[dict]:
    """
    Détecte la méthode pédagogique Oryx pertinente pour une question.

    Pipeline :
    1. Normalisation question (lowercase + suppression accents)
    2. Détection sous-méthodes prioritaires (4 sous-piliers qualitatifs)
    3. Détection méthodes principales (bilan, ratios, qualitatif chapeau)
    4. Retourne le dict de la méthode (avec method_id ajouté) ou None

    Args:
        question: question utilisateur brute
        context: historique conversationnel (optionnel, non utilisé pour l'instant
                 mais accepté pour évolution future)

    Returns:
        dict contenant method_id, title, method_content, example_company,
        keywords_matched, OU None si aucun match.
    """
    if not question:
        print("[PEDAGOGIE] empty question, no match")
        return None

    normalized = _normalize(question)

    # Question trop courte : pas de matching fiable
    if len(normalized.split()) < 2:
        print(f"[PEDAGOGIE] question too short ('{question}'), no match")
        return None

    # 1. Recherche dans les sous-méthodes prioritaires
    for method_id in SOUS_METHODES_PRIORITAIRES:
        method = METHODES[method_id]
        matched_keywords = [kw for kw in method["keywords"] if _normalize(kw) in normalized]
        if matched_keywords:
            print(f"[PEDAGOGIE] '{question}' → method_id='{method_id}' (matched: {matched_keywords})")
            return {
                "method_id": method_id,
                "title": method["title"],
                "method_content": method["method_content"],
                "example_company": method["example_company"],
                "keywords_matched": matched_keywords,
            }

    # 2. Recherche dans les méthodes principales
    for method_id in METHODES_PRINCIPALES:
        method = METHODES[method_id]
        matched_keywords = [kw for kw in method["keywords"] if _normalize(kw) in normalized]
        if matched_keywords:
            print(f"[PEDAGOGIE] '{question}' → method_id='{method_id}' (matched: {matched_keywords})")
            return {
                "method_id": method_id,
                "title": method["title"],
                "method_content": method["method_content"],
                "example_company": method["example_company"],
                "keywords_matched": matched_keywords,
            }

    # 3. Aucun match
    print(f"[PEDAGOGIE] '{question}' → no match, Claude répondra librement")
    return None
