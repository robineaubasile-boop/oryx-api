"""SPEC canonique Oryx Taxonomy V1 (T4-C) : les 12 compétences, les 8
règles globales de mapping et les 45 capacités noyau, leur validation
statique stricte, leur canonicalisation déterministe et leur fingerprint
SHA-256.

Python pur (bibliothèque standard) : aucune Session SQLAlchemy, aucune
écriture, rien n'est exécuté à l'import. L'écriture en base passe par
core/pedagogy/taxonomy_bootstrap.py, au-dessus du service T4-B.

Doctrine. Observation = force et profondeur de la preuve ; capability =
périmètre sémantique de cette preuve. Les 45 capacités ne sont ni 45
compétences utilisateur, ni des sous-niveaux, ni une checklist : aucune ne
porte de stage, de score, de confiance, de progression, de statut acquis ni
de compteur de preuves. Une observation C7 localisée sur C7_A et C7_B reste
UNE observation ; elle ne se localise jamais sur C8_*. Cette SPEC ne décrit
que des périmètres : elle ne fait aucune inférence sur un utilisateur.

Pourquoi pas une migration. Alembic porte la STRUCTURE (0007, tables
vides) ; ce module porte le CONTENU sémantique versionné, inséré par un
bootstrap applicatif contrôlé (jamais au pre-deploy). Un changement de
contenu est une nouvelle révision sémantique / une nouvelle release, pas un
changement de schéma.

Contenu. Les libellés, définitions, questions centrales, règles globales et
mapping_guidance sont CANONIQUES : ils sont recopiés tels quels de la SPEC
métier Oryx, sans paraphrase ni normalisation (Unicode préservé, apostrophe
ASCII, aucun NFC/NFD appliqué). Toute modification change
EXPECTED_V1_FINGERPRINT et doit être une décision pédagogique explicite :
pour une définition déjà persistée, une modification de sens est une
semantic_revision 2 dans une future release (oryx-v2), jamais une réécriture
de la révision 1.

Persisté vs fingerprinté. Seules les 45 capacités sont persistées
(core_capability_definitions + memberships). Les 12 compétences et les 8
règles globales n'ont pas de table (aucune n'est créée en T4-C) mais font
partie du payload canonique : spec_fingerprint identifie donc le
référentiel ENTIER, pas seulement la concaténation des 45 lignes.

Fingerprint : SHA-256 (hex minuscule, 64 caractères) de

    json.dumps(payload, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False, allow_nan=False).encode("utf-8")

où payload = {taxonomy_schema_version, version_key, global_mapping_rules,
competencies, capabilities}, les trois listes étant triées dans l'ordre
naturel (R1..R8, C1..C12, C1_A..C12_D : C10 après C9, jamais l'ordre
lexical). L'ordre des éléments DANS include / exclude / boundary_notes est
conservé (il fait partie du contenu). Aucun identifiant de base, horodatage
ou statut n'entre dans le payload : le fingerprint est identique entre
processus, machines et environnements.

V2 future (non implémentée) : oryx-v2 réutilisera les définitions r1 dont
le sens est inchangé (même ligne, même UUID) et n'introduira une r2 que pour
les capacités dont le sens change ; oryx-v1 et ses memberships restent
intacts.
"""
import copy
import hashlib
import json
import math
import re

TAXONOMY_SCHEMA_VERSION = 1
VERSION_KEY = "oryx-v1"
V1_SEMANTIC_REVISION = 1

# SHA-256 du JSON canonique de la SPEC ci-dessous, calculé à partir de la
# SPEC implémentée puis figé (voir tests/test_taxonomy_v1.py). Ne jamais le
# modifier pour « faire passer » un test : un nouveau fingerprint signifie
# un nouveau contenu pédagogique, donc une décision explicite.
EXPECTED_V1_FINGERPRINT = "50b690b9e5ddd6812b625b02037b6f29b8b6cd553c3cf7d4069a634f7df5e2f7"

# Les 45 codes figés par T4-A (CHECK ck_core_capability_definitions_capability_code),
# dans l'ordre naturel. Recopie volontaire (ce module ne dépend pas du
# service T4-B) ; tests/test_taxonomy_v1.py garantit l'égalité exacte avec
# core.taxonomy_service et la contrainte de 0007.
CAPABILITY_CODES = (
    "C1_A", "C1_B", "C1_C",
    "C2_A", "C2_B", "C2_C",
    "C3_A", "C3_B", "C3_C",
    "C4_A", "C4_B", "C4_C", "C4_D",
    "C5_A", "C5_B", "C5_C", "C5_D",
    "C6_A", "C6_B", "C6_C", "C6_D",
    "C7_A", "C7_B", "C7_C", "C7_D",
    "C8_A", "C8_B", "C8_C", "C8_D",
    "C9_A", "C9_B", "C9_C", "C9_D",
    "C10_A", "C10_B", "C10_C", "C10_D",
    "C11_A", "C11_B", "C11_C", "C11_D",
    "C12_A", "C12_B", "C12_C", "C12_D",
)
COMPETENCY_CODES = tuple(f"C{i}" for i in range(1, 13))
GLOBAL_RULE_CODES = tuple(f"R{i}" for i in range(1, 9))

SPEC_KEYS = frozenset({"taxonomy_schema_version", "version_key", "global_mapping_rules",
                       "competencies", "capabilities"})
RULE_KEYS = frozenset({"rule_code", "label", "rule"})
COMPETENCY_KEYS = frozenset({"competency_code", "label", "central_question"})
CAPABILITY_KEYS = frozenset({"capability_code", "semantic_revision", "competency_code", "label",
                             "definition", "mapping_guidance"})
GUIDANCE_KEYS = frozenset({"include", "exclude", "boundary_notes"})
BOUNDARY_NOTE_KEYS = frozenset({"against", "rule"})

# Champs d'une capacité persistés dans core_capability_definitions.
PERSISTED_CAPABILITY_FIELDS = ("capability_code", "semantic_revision", "competency_code", "label",
                               "definition", "mapping_guidance")


class TaxonomySpecError(Exception):
    """SPEC invalide (structure, vocabulaire, identité V1) ou fingerprint
    différent de EXPECTED_V1_FINGERPRINT. Toujours levée avant toute
    écriture en base."""


# --------------------------------------------------------------------------
# Données canoniques
# --------------------------------------------------------------------------

def _rule(rule_code, label, rule):
    return {"rule_code": rule_code, "label": label, "rule": rule}


def _competency(competency_code, label, central_question):
    return {"competency_code": competency_code, "label": label, "central_question": central_question}


def _capability(capability_code, label, definition, include, exclude, boundary_notes=()):
    return {
        "capability_code": capability_code,
        "semantic_revision": V1_SEMANTIC_REVISION,
        "competency_code": capability_code.split("_", 1)[0],
        "label": label,
        "definition": definition,
        "mapping_guidance": {
            "include": list(include),
            "exclude": list(exclude),
            "boundary_notes": [{"against": against, "rule": rule} for against, rule in boundary_notes],
        },
    }


_GLOBAL_MAPPING_RULES = [
    _rule("R1", "Capability != preuve",
          "Un mapping ne crée jamais une observation supplémentaire."),
    _rule("R2", "Capability != stade",
          "Aucune capability ne possède Discovery / Comprehension / Application / Mastery."),
    _rule("R3", "Pas de comptage mécanique",
          "Le nombre de capabilities couvertes ne détermine jamais un stage."),
    _rule("R4", "Plusieurs capabilities possibles",
          "Une observation Cx peut mapper plusieurs Cx_* si le même raisonnement couvre réellement "
          "plusieurs dimensions."),
    _rule("R5", "Jamais cross-competency",
          "Une observation C7 ne mappe jamais C8_A. Si le raisonnement démontre C7 et C8, produire deux "
          "observations."),
    _rule("R6", "competency_only est légitime",
          "Si Cx est clair mais Cx_A/B/C/D n'est pas localisable proprement, ne rien inventer."),
    _rule("R7", "Raisonnement > mot-clé",
          "Mentionner ROIC, FCF, moat, diversification, PER, etc. ne suffit jamais à mapper une capability."),
    _rule("R8", "Minimum nécessaire",
          "Mapper uniquement les capabilities réellement démontrées par le raisonnement."),
]

_COMPETENCIES = [
    _competency("C1", "Fondations de l'investissement",
                "L'utilisateur comprend-il ce qu'il est réellement en train de faire lorsqu'il investit ?"),
    _competency("C2", "Risque & horizon",
                "L'utilisateur sait-il relier le risque pris à son horizon, à sa liquidité et à sa capacité "
                "réelle à supporter une perte ?"),
    _competency("C3", "Diversification & allocation",
                "L'utilisateur comprend-il comment le capital est réparti entre différentes expositions et "
                "sait-il identifier les concentrations visibles ou cachées ?"),
    _competency("C4", "Compréhension du business",
                "L'utilisateur comprend-il comment l'entreprise crée de la valeur pour ses clients, transforme "
                "cette activité en revenus, de quoi son modèle dépend et pourquoi sa position peut être "
                "défendable ?"),
    _competency("C5", "Croissance",
                "L'utilisateur comprend-il ce qui fait réellement croître l'entreprise, d'où vient cette "
                "croissance, dans quelle mesure elle peut durer et ce qu'il faut éviter d'extrapoler ?"),
    _competency("C6", "Rentabilité",
                "L'utilisateur comprend-il comment l'entreprise transforme son activité en profit, pourquoi "
                "cette rentabilité évolue et dans quelle mesure elle est durable ?"),
    _competency("C7", "Cash-flow",
                "L'utilisateur comprend-il comment l'activité se transforme en cash, pourquoi bénéfice et "
                "trésorerie divergent, où le cash est absorbé et quelle part reste disponible ?"),
    _competency("C8", "Efficacité du capital / ROIC",
                "L'utilisateur comprend-il combien de résultat économique l'entreprise obtient du capital "
                "mobilisé, comment cette efficacité évolue lorsqu'elle réinvestit et si l'allocation de ce "
                "capital crée de la valeur ?"),
    _competency("C9", "Bilan & solidité financière",
                "L'utilisateur comprend-il comment l'entreprise est financée, quelles obligations elle doit "
                "supporter, si elle dispose des ressources nécessaires et comment sa structure résisterait si "
                "l'environnement se détériorait ?"),
    _competency("C10", "Valorisation",
                "L'utilisateur comprend-il comment relier le prix payé aux fondamentaux futurs, quelles "
                "hypothèses rendent ce prix plausible et comment raisonner malgré l'incertitude ?"),
    _competency("C11", "Thèse & risques",
                "L'utilisateur sait-il formuler ce qui doit être vrai pour que son raisonnement reste valable, "
                "identifier ce qui pourrait le contredire, distinguer le bruit et modifier sa thèse lorsque "
                "les faits changent ?"),
    _competency("C12", "Raisonnement portefeuille",
                "L'utilisateur comprend-il comment les positions, leurs rôles, leurs thèses et leurs "
                "dépendances interagissent à l'échelle du portefeuille ?"),
]

_CAPABILITIES = [
    # ------------------------------------------------------------------ C1
    _capability(
        "C1_A", "Nature économique de l'investissement",
        "Comprendre que l'investissement est une allocation de capital vers des actifs dont la valeur future "
        "dépend d'une réalité économique incertaine.",
        include=[
            "Raisonne sur l'investissement comme allocation de capital.",
            "Relie la valeur future à une réalité économique incertaine.",
            "Distingue investir d'un simple pari sur un mouvement de prix.",
        ],
        exclude=[
            "Simple connaissance du fonctionnement d'un compte ou d'une enveloppe.",
            "Simple commentaire sur la hausse ou la baisse du cours.",
        ],
    ),
    _capability(
        "C1_B", "Actif / enveloppe / intermédiaire",
        "Savoir distinguer ce que l'on possède, le cadre dans lequel on le détient et l'acteur qui permet la "
        "transaction.",
        include=[
            "Distingue l'actif détenu, l'enveloppe de détention et l'intermédiaire.",
            "Comprend qu'un PEA, CTO ou autre enveloppe n'est pas l'investissement lui-même.",
        ],
        exclude=[
            "Analyse de la qualité économique de l'actif.",
            "Analyse du mouvement du prix.",
        ],
    ),
    _capability(
        "C1_C", "Fondamentaux / mouvement de prix",
        "Comprendre qu'un mouvement de marché n'est pas en lui-même une création ou une destruction de valeur "
        "fondamentale et distinguer un raisonnement d'investissement d'un simple pari sur le prix.",
        include=[
            "Distingue évolution du cours et évolution des fondamentaux.",
            "Refuse d'utiliser le mouvement du prix comme preuve économique suffisante.",
        ],
        exclude=[
            "Suivi d'une thèse déjà constituée face à une nouvelle information.",
        ],
        boundary_notes=[
            ("C11_C", "C1_C porte sur le principe prix/fondamentaux ; C11_C applique cette distinction au "
                      "suivi d'une thèse."),
        ],
    ),
    # ------------------------------------------------------------------ C2
    _capability(
        "C2_A", "Nature du risque",
        "Comprendre que le risque ne se réduit ni à la volatilité ni à une seule forme de perte, et savoir "
        "distinguer variation temporaire, incertitude, contraintes de liquidité et possibilité de perte "
        "durable.",
        include=[
            "Distingue plusieurs natures de risque.",
            "Distingue variation temporaire et perte durable.",
            "Raisonne en incertitude plutôt qu'en volatilité seule.",
        ],
        exclude=[
            "Calendrier personnel du besoin de capital.",
            "Capacité financière personnelle à absorber une perte.",
        ],
    ),
    _capability(
        "C2_B", "Horizon & liquidité",
        "Savoir relier le risque acceptable à la durée pendant laquelle le capital peut rester investi et au "
        "moment où il pourrait être nécessaire.",
        include=[
            "Relie le risque au temps pendant lequel le capital peut rester investi.",
            "Considère le moment où le capital pourrait être nécessaire.",
        ],
        exclude=[
            "Simple préférence psychologique pour le risque.",
            "Échéances de dette d'une entreprise.",
        ],
    ),
    _capability(
        "C2_C", "Capacité vs tolérance au risque",
        "Savoir distinguer la volonté psychologique de supporter une baisse de la capacité financière réelle "
        "à en supporter les conséquences.",
        include=[
            "Distingue volonté psychologique de supporter une baisse et capacité financière réelle.",
            "Raisonne sur les conséquences financières personnelles d'une perte.",
        ],
        exclude=[
            "Analyse de la solidité financière d'une entreprise.",
            "Simple description de volatilité.",
        ],
    ),
    # ------------------------------------------------------------------ C3
    _capability(
        "C3_A", "Poids & concentration",
        "Comprendre qu'un portefeuille se définit par les poids de ses positions et savoir identifier "
        "lorsqu'une ou plusieurs expositions deviennent dominantes.",
        include=[
            "Raisonne sur le poids d'une ou plusieurs positions.",
            "Détecte qu'une exposition devient dominante.",
        ],
        exclude=[
            "Dépendance économique commune entre plusieurs thèses.",
        ],
        boundary_notes=[
            ("C12_B", "Un poids important relève de C3_A ; plusieurs positions dépendant d'un même cycle ou "
                      "facteur économique relèvent de C12_B."),
        ],
    ),
    _capability(
        "C3_B", "Diversification réelle des expositions",
        "Comprendre que le nombre de lignes, de secteurs ou d'ETF ne garantit pas une diversification réelle "
        "et savoir repérer les chevauchements ou concentrations visibles.",
        include=[
            "Comprend que nombre de lignes, secteurs ou ETF ne garantit pas la diversification.",
            "Détecte chevauchements ou concentrations visibles dans les expositions.",
        ],
        exclude=[
            "Analyse causale des hypothèses économiques communes entre plusieurs positions.",
        ],
        boundary_notes=[
            ("C12_B", "C3_B traite les chevauchements et concentrations visibles ; C12_B traite les "
                      "dépendances économiques ou hypothèses communes entre positions."),
        ],
    ),
    _capability(
        "C3_C", "Mécanique d'allocation",
        "Comprendre comment la répartition du capital et les changements de poids modifient l'exposition "
        "globale du portefeuille, sans recourir à des seuils universels ou à une allocation prescrite.",
        include=[
            "Comprend comment la répartition du capital modifie les expositions.",
            "Raisonne sur les conséquences mécaniques d'un changement de poids.",
            "Évite les seuils universels de pondération.",
        ],
        exclude=[
            "Effet causal d'une modification de thèse sur l'ensemble du portefeuille.",
        ],
        boundary_notes=[
            ("C12_D", "C3_C traite la mécanique de répartition ; C12_D traite les conséquences systémiques "
                      "d'un changement dans le portefeuille."),
        ],
    ),
    # ------------------------------------------------------------------ C4
    _capability(
        "C4_A", "Client & proposition de valeur",
        "Comprendre ce que l'entreprise vend, à qui et pourquoi le client accepte de payer.",
        include=[
            "Identifie ce que l'entreprise vend.",
            "Identifie qui paie.",
            "Explique pourquoi le client accepte de payer.",
        ],
        exclude=[
            "Mécanisme financier transformant l'activité en chiffre d'affaires.",
            "Avantage concurrentiel à lui seul.",
        ],
    ),
    _capability(
        "C4_B", "Moteur de revenus",
        "Comprendre comment l'activité se transforme économiquement en chiffre d'affaires et quelles variables "
        "principales pilotent ce revenu.",
        include=[
            "Explique comment l'activité produit du chiffre d'affaires.",
            "Identifie les variables économiques principales qui pilotent les revenus.",
        ],
        exclude=[
            "Simple description des produits.",
            "Décomposition du taux de croissance historique sans raisonnement sur le moteur de revenus.",
        ],
    ),
    _capability(
        "C4_C", "Chaîne de valeur & dépendances",
        "Comprendre les acteurs, ressources, partenaires, canaux ou contraintes dont le fonctionnement du "
        "business dépend.",
        include=[
            "Identifie acteurs, ressources, partenaires, canaux ou contraintes nécessaires au business.",
            "Raisonne sur les dépendances opérationnelles du modèle.",
        ],
        exclude=[
            "Risque formulé comme condition d'invalidation d'une thèse.",
        ],
        boundary_notes=[
            ("C11_B", "C4_C décrit de quoi dépend le fonctionnement du business ; C11_B détermine quelles "
                      "dépendances ou hypothèses peuvent fragiliser ou invalider une thèse."),
        ],
    ),
    _capability(
        "C4_D", "Position concurrentielle & moat",
        "Comprendre pourquoi la position d'une entreprise peut être défendable ou fragile et rechercher le "
        "mécanisme économique derrière l'avantage concurrentiel plutôt que l'inférer mécaniquement d'un "
        "indicateur financier.",
        include=[
            "Cherche pourquoi une position concurrentielle est défendable ou fragile.",
            "Identifie le mécanisme économique derrière l'avantage.",
        ],
        exclude=[
            "Infère automatiquement un moat d'une marge ou d'un ROIC élevé.",
            "Simple constat de croissance.",
        ],
    ),
    # ------------------------------------------------------------------ C5
    _capability(
        "C5_A", "Nature & décomposition de la croissance",
        "Savoir identifier ce qui croît et distinguer les principales sources de croissance : organique, "
        "acquisition, prix, volume, mix ou autres moteurs pertinents au business.",
        include=[
            "Identifie ce qui croît.",
            "Décompose croissance organique, acquisitions, prix, volumes, mix ou autres moteurs pertinents.",
        ],
        exclude=[
            "Jugement sur la durabilité future sans décomposition.",
            "Conséquences de la croissance sur le capital nécessaire.",
        ],
    ),
    _capability(
        "C5_B", "Moteurs & durabilité",
        "Comprendre pourquoi l'entreprise croît et raisonner sur la capacité des moteurs actuels à se "
        "prolonger.",
        include=[
            "Explique pourquoi l'entreprise croît.",
            "Examine si les moteurs actuels peuvent se prolonger.",
        ],
        exclude=[
            "Extrapolation pure du taux historique.",
            "Analyse détaillée du rendement du capital nécessaire à cette croissance.",
        ],
    ),
    _capability(
        "C5_C", "Normalisation & extrapolation",
        "Savoir reconnaître effets de base, cyclicité, événements exceptionnels et limites structurelles afin "
        "de ne pas projeter mécaniquement la croissance passée.",
        include=[
            "Identifie effets de base.",
            "Identifie cyclicité ou événements exceptionnels.",
            "Refuse de projeter mécaniquement la croissance passée.",
        ],
        exclude=[
            "Normalisation des marges.",
            "Normalisation du cash-flow ou du ROIC.",
        ],
    ),
    _capability(
        "C5_D", "Qualité de la croissance",
        "Comprendre qu'une croissance doit aussi être examinée au regard de ce qu'elle nécessite pour être "
        "obtenue, sans assimiler mécaniquement croissance rapide et création de valeur.",
        include=[
            "Examine ce que la croissance nécessite pour être obtenue.",
            "Distingue croissance rapide et création de valeur.",
        ],
        exclude=[
            "Mesure précise des conséquences sur marges, cash ou rendement du capital.",
        ],
        boundary_notes=[
            ("C6-C8", "C5 qualifie la croissance ; C6, C7 et C8 analysent respectivement ses conséquences sur "
                      "profit, cash et capital."),
        ],
    ),
    # ------------------------------------------------------------------ C6
    _capability(
        "C6_A", "Structure de la rentabilité",
        "Savoir distinguer les principaux niveaux de marge et comprendre ce qu'ils révèlent - ou ne révèlent "
        "pas - sur l'économie de l'entreprise.",
        include=[
            "Distingue les principaux niveaux de marge.",
            "Comprend ce que ces marges révèlent ou ne révèlent pas.",
        ],
        exclude=[
            "Assimilation du profit au cash.",
            "Assimilation de la marge au ROIC.",
        ],
    ),
    _capability(
        "C6_B", "Moteurs des marges",
        "Savoir identifier pourquoi la rentabilité s'améliore ou se détériore : prix, mix, coûts, volumes, "
        "efficacité ou autres facteurs pertinents.",
        include=[
            "Explique variation des marges par prix, mix, coûts, volumes, efficacité ou autre moteur pertinent.",
        ],
        exclude=[
            "Simple constat qu'une marge augmente ou diminue.",
            "Effet mécanique spécifique des coûts fixes et variables sans raisonnement plus large.",
        ],
    ),
    _capability(
        "C6_C", "Structure de coûts & levier opérationnel",
        "Comprendre comment coûts fixes et variables influencent l'évolution des profits lorsque l'activité "
        "augmente ou diminue.",
        include=[
            "Distingue coûts fixes et variables.",
            "Raisonne sur l'effet des variations d'activité sur les profits.",
        ],
        exclude=[
            "Simple évolution observée d'une marge sans mécanisme de coûts.",
        ],
    ),
    _capability(
        "C6_D", "Normalisation & durabilité",
        "Savoir distinguer une rentabilité structurelle d'un niveau influencé par le cycle, des événements "
        "exceptionnels ou des conditions temporairement favorables ou défavorables.",
        include=[
            "Distingue rentabilité structurelle et temporaire.",
            "Tient compte du cycle et des éléments exceptionnels.",
        ],
        exclude=[
            "Normalisation du FCF.",
            "Normalisation du rendement du capital.",
        ],
    ),
    # ------------------------------------------------------------------ C7
    _capability(
        "C7_A", "Résultat comptable vs cash",
        "Comprendre pourquoi bénéfice et génération de trésorerie peuvent diverger et ne jamais assimiler "
        "automatiquement résultat comptable à cash disponible.",
        include=[
            "Distingue résultat comptable et génération de trésorerie.",
            "Cherche pourquoi les deux divergent.",
        ],
        exclude=[
            "Attribue spécifiquement l'écart au BFR lorsque ce mécanisme est déjà identifié.",
            "Juge le rendement du capital.",
        ],
        boundary_notes=[
            ("C7_B", "C7_A couvre la divergence résultat/cash ; C7_B localise cette divergence dans le BFR "
                     "lorsque ce mécanisme est effectivement raisonné."),
        ],
    ),
    _capability(
        "C7_B", "BFR & conversion opérationnelle",
        "Comprendre comment stocks, créances et dettes d'exploitation peuvent absorber ou libérer du cash et "
        "savoir en rechercher la cause économique.",
        include=[
            "Raisonne sur stocks, créances et dettes d'exploitation.",
            "Explique comment le BFR absorbe ou libère du cash.",
            "Recherche la cause économique de cette évolution.",
        ],
        exclude=[
            "Simple constat que résultat comptable et cash diffèrent sans identifier le BFR.",
            "CAPEX.",
        ],
        boundary_notes=[
            ("C7_A", "C7_A couvre la divergence résultat/cash ; C7_B localise cette divergence dans le BFR "
                     "lorsque ce mécanisme est effectivement raisonné."),
        ],
    ),
    _capability(
        "C7_C", "CAPEX & construction du FCF",
        "Comprendre comment les investissements nécessaires transforment le cash opérationnel en cash "
        "réellement disponible et distinguer, lorsque le contexte le permet, maintenance et croissance.",
        include=[
            "Relie cash opérationnel, investissements et FCF.",
            "Distingue lorsque pertinent CAPEX de maintenance et de croissance.",
        ],
        exclude=[
            "Juge le rendement économique produit par le capital investi.",
        ],
        boundary_notes=[
            ("C8", "C7_C traite le CAPEX comme flux de cash ; C8 juge l'efficacité et le rendement du capital "
                   "mobilisé."),
        ],
    ),
    _capability(
        "C7_D", "Qualité & normalisation du cash-flow",
        "Savoir distinguer une capacité durable de génération de cash d'un FCF temporairement influencé par "
        "le BFR, les investissements, le cycle ou des éléments exceptionnels.",
        include=[
            "Distingue génération durable et FCF temporairement perturbé.",
            "Examine BFR, investissements, cycle et éléments exceptionnels.",
        ],
        exclude=[
            "Simple calcul du FCF.",
            "Normalisation du ROIC.",
        ],
    ),
    # ------------------------------------------------------------------ C8
    _capability(
        "C8_A", "Rendement du capital investi",
        "Comprendre que la rentabilité doit être mise en relation avec le capital nécessaire pour la produire "
        "et savoir distinguer marge, ROE et efficacité économique du capital.",
        include=[
            "Relie résultat économique et capital nécessaire.",
            "Distingue marge, ROE et efficacité économique du capital.",
        ],
        exclude=[
            "Simple marge élevée.",
            "Simple génération de cash.",
        ],
    ),
    _capability(
        "C8_B", "Intensité capitalistique & réinvestissement",
        "Comprendre combien de capital supplémentaire est nécessaire pour soutenir la croissance et pourquoi "
        "deux croissances identiques peuvent avoir une économie très différente.",
        include=[
            "Examine le capital supplémentaire nécessaire à la croissance.",
            "Compare l'économie de deux croissances nécessitant des niveaux de capital différents.",
        ],
        exclude=[
            "Simple taux de croissance.",
            "Simple CAPEX comme sortie de cash.",
        ],
    ),
    _capability(
        "C8_C", "Rendement incrémental & durabilité",
        "Savoir examiner ce que produisent les nouveaux investissements, normaliser le ROIC et distinguer un "
        "rendement structurel d'un niveau temporaire ou comptablement trompeur.",
        include=[
            "Examine ce que produisent les nouveaux investissements.",
            "Distingue rendement structurel et niveau temporaire ou comptablement trompeur.",
            "Raisonne sur le rendement incrémental.",
        ],
        exclude=[
            "ROIC historique utilisé isolément.",
            "Simple décision sur l'usage du capital.",
        ],
    ),
    _capability(
        "C8_D", "Allocation du capital",
        "Comprendre les différents usages possibles du capital et évaluer leur logique économique selon les "
        "opportunités disponibles, sans considérer réinvestissement, acquisitions, dividendes ou rachats comme "
        "intrinsèquement bons ou mauvais.",
        include=[
            "Compare réinvestissement, acquisitions, dividendes, rachats ou autres usages.",
            "Évalue leur logique économique selon les opportunités disponibles.",
            "Refuse de considérer un usage comme intrinsèquement bon ou mauvais.",
        ],
        exclude=[
            "Juge le prix payé pour l'action.",
        ],
        boundary_notes=[
            ("C10", "C8_D juge l'usage du capital par l'entreprise ; C10 juge le prix payé et les hypothèses "
                    "incorporées dans ce prix."),
        ],
    ),
    # ------------------------------------------------------------------ C9
    _capability(
        "C9_A", "Structure financière & bilan",
        "Comprendre comment trésorerie, dette, capitaux propres et principaux engagements composent la "
        "structure financière de l'entreprise, sans interpréter isolément dette brute ou cash.",
        include=[
            "Raisonne conjointement sur cash, dette, capitaux propres et engagements.",
            "Refuse d'interpréter dette brute ou cash isolément.",
        ],
        exclude=[
            "Conclut sur le risque global de l'investissement uniquement à partir du bilan.",
        ],
    ),
    _capability(
        "C9_B", "Liquidité, service de la dette & échéances",
        "Savoir relier les obligations financières à leur calendrier, au coût de financement et aux "
        "ressources réellement disponibles pour y faire face.",
        include=[
            "Relie obligations financières, calendrier, coût de financement et ressources disponibles.",
        ],
        exclude=[
            "Horizon personnel de l'investisseur.",
            "Simple montant de dette.",
        ],
    ),
    _capability(
        "C9_C", "Résilience & stress financier",
        "Savoir examiner comment la structure financière résisterait à une détérioration des résultats, du "
        "cash-flow ou des conditions de financement, sans utiliser de seuil universel.",
        include=[
            "Teste la structure financière sous détérioration des résultats, du cash ou du financement.",
            "Raisonne sans seuil universel.",
        ],
        exclude=[
            "Stress économique de plusieurs positions du portefeuille.",
        ],
    ),
    _capability(
        "C9_D", "Financement externe & dilution",
        "Comprendre comment une entreprise peut financer ses besoins lorsqu'elle ne génère pas suffisamment de "
        "ressources internes et quelles conséquences dette, émissions d'actions ou autres financements peuvent "
        "avoir sur sa solidité et ses actionnaires.",
        include=[
            "Raisonne sur le financement nécessaire lorsque les ressources internes sont insuffisantes.",
            "Analyse dette, émission d'actions et dilution pour l'actionnaire.",
        ],
        exclude=[
            "Valorisation de l'action à partir de cette dilution.",
        ],
        boundary_notes=[
            ("C10", "C9_D traite le mécanisme de financement et ses conséquences financières ; C10 traite son "
                    "intégration dans la valeur ou le prix par action."),
        ],
    ),
    # ----------------------------------------------------------------- C10
    _capability(
        "C10_A", "Prix relatif aux fondamentaux",
        "Comprendre qu'un prix ou un multiple n'a de sens qu'en relation avec la croissance, la rentabilité, "
        "le cash-flow, le risque et la qualité économique du business.",
        include=[
            "Relie prix ou multiple à croissance, rentabilité, cash-flow, risque et qualité économique.",
            "Refuse d'interpréter un multiple isolément.",
        ],
        exclude=[
            "Simple distinction fondamentale prix/valeur de C1.",
            "Construction détaillée des hypothèses implicites.",
        ],
    ),
    _capability(
        "C10_B", "Hypothèses implicites & reverse valuation",
        "Savoir identifier les hypothèses de croissance, marges, cash-flow, durée et risque nécessaires pour "
        "justifier un prix, plutôt que traiter une estimation de valeur comme une vérité précise.",
        include=[
            "Recherche quelles hypothèses de croissance, marges, cash, durée ou risque justifient le prix.",
            "Raisonne du prix vers les attentes implicites.",
        ],
        exclude=[
            "Traite une estimation de valeur comme une vérité précise.",
            "Simple sélection technique d'une méthode.",
        ],
    ),
    _capability(
        "C10_C", "Méthodes & cohérence de valorisation",
        "Savoir sélectionner et interpréter une méthode adaptée au business, maintenir la cohérence des "
        "métriques utilisées et comparer plusieurs approches ou scénarios sans transformer leur convergence en "
        "certitude.",
        include=[
            "Choisit une méthode adaptée au business.",
            "Maintient la cohérence des métriques.",
            "Compare méthodes ou scénarios sans transformer leur convergence en certitude.",
        ],
        exclude=[
            "Choix d'un multiple isolé sans cohérence économique.",
        ],
    ),
    _capability(
        "C10_D", "Incertitude, scénarios & rendement conditionnel",
        "Savoir raisonner en fourchettes et scénarios, relier le prix payé aux rendements possibles sous "
        "différentes hypothèses et utiliser éventuellement une marge de sécurité comme réponse à "
        "l'incertitude, sans seuil universel.",
        include=[
            "Raisonne en fourchettes et scénarios.",
            "Relie prix payé et rendements possibles sous différentes hypothèses.",
            "Utilise éventuellement une marge de sécurité comme réponse à l'incertitude.",
        ],
        exclude=[
            "Seuil universel de marge de sécurité.",
            "Fair value présentée comme valeur exacte.",
        ],
    ),
    # ----------------------------------------------------------------- C11
    _capability(
        "C11_A", "Thèse explicite & testable",
        "Savoir transformer l'analyse en quelques hypothèses causales et structurantes expliquant ce qui doit "
        "se produire pour que le raisonnement d'investissement reste cohérent.",
        include=[
            "Transforme l'analyse en hypothèses causales structurantes.",
            "Explicite ce qui doit se produire pour que le raisonnement reste cohérent.",
        ],
        exclude=[
            "Simple liste de qualités de l'entreprise.",
            "Simple résumé des analyses C4-C10.",
        ],
    ),
    _capability(
        "C11_B", "Risques, hypothèses critiques & falsifiabilité",
        "Savoir identifier ce qui pourrait réellement fragiliser ou invalider les hypothèses centrales et "
        "distinguer un risque générique d'un risque directement lié à la thèse.",
        include=[
            "Identifie ce qui pourrait fragiliser ou invalider les hypothèses centrales.",
            "Distingue risque générique et risque directement lié à la thèse.",
        ],
        exclude=[
            "Simple dépendance opérationnelle du business sans lien avec la thèse.",
            "Baisse du cours traitée automatiquement comme invalidation.",
        ],
        boundary_notes=[
            ("C4_C", "C4_C décrit les dépendances du business ; C11_B identifie lesquelles peuvent fragiliser "
                     "ou invalider la thèse."),
        ],
    ),
    _capability(
        "C11_C", "Signal fondamental vs bruit de marché",
        "Savoir distinguer mouvement de prix, information nouvelle et modification réelle des fondamentaux, "
        "sans utiliser le marché lui-même comme preuve que la thèse est correcte ou incorrecte.",
        include=[
            "Distingue mouvement du prix, information nouvelle et modification réelle des fondamentaux.",
            "Examine si une information change réellement la thèse.",
        ],
        exclude=[
            "Principe élémentaire prix/fondamentaux sans application à une thèse.",
        ],
        boundary_notes=[
            ("C1_C", "C1_C établit la distinction fondamentale ; C11_C l'utilise pour décider si une thèse doit "
                     "être affectée."),
        ],
    ),
    _capability(
        "C11_D", "Révision de la thèse",
        "Savoir confronter une thèse passée aux nouvelles informations, identifier ce qui est confirmé, "
        "affaibli ou invalidé et modifier son raisonnement lorsque les faits l'exigent.",
        include=[
            "Confronte explicitement une thèse passée aux nouvelles informations.",
            "Identifie ce qui est confirmé, affaibli ou invalidé.",
            "Modifie rationnellement le raisonnement lorsque les faits changent.",
        ],
        exclude=[
            "Réaction mécanique au cours.",
            "Simple production d'une nouvelle analyse sans confrontation à la thèse antérieure.",
        ],
    ),
    # ----------------------------------------------------------------- C12
    _capability(
        "C12_A", "Rôle des positions",
        "Comprendre pourquoi chaque position existe dans l'ensemble et quelle exposition ou fonction "
        "économique elle apporte au portefeuille.",
        include=[
            "Explique pourquoi une position existe dans l'ensemble.",
            "Identifie son exposition ou sa fonction économique dans le portefeuille.",
        ],
        exclude=[
            "Simple analyse isolée de la qualité de l'entreprise.",
            "Simple poids de la position.",
        ],
    ),
    _capability(
        "C12_B", "Dépendances & hypothèses communes",
        "Savoir identifier les facteurs économiques, risques ou hypothèses partagés entre plusieurs positions, "
        "y compris lorsqu'ils ne sont pas visibles à travers les secteurs ou les instruments détenus.",
        include=[
            "Identifie facteurs économiques, risques ou hypothèses partagés par plusieurs positions.",
            "Détecte des dépendances invisibles à travers secteurs ou instruments.",
        ],
        exclude=[
            "Simple chevauchement visible ou concentration mécanique.",
        ],
        boundary_notes=[
            ("C3", "C3 décrit la structure visible et mécanique ; C12_B décrit les dépendances économiques et "
                   "causales communes."),
        ],
    ),
    _capability(
        "C12_C", "Interaction des thèses & stress test portefeuille",
        "Savoir raisonner sur les conséquences qu'un changement de scénario ou de fondamentaux peut avoir "
        "simultanément sur plusieurs thèses et distinguer les positions réellement affectées.",
        include=[
            "Examine comment un même scénario ou changement fondamental affecte simultanément plusieurs thèses.",
            "Distingue quelles positions sont réellement affectées et pourquoi.",
        ],
        exclude=[
            "Stress financier d'une seule entreprise.",
            "Simple constat d'exposition commune sans analyse des thèses.",
        ],
    ),
    _capability(
        "C12_D", "Conséquence globale des changements de portefeuille",
        "Savoir analyser comment un changement de poids, l'ajout ou le retrait d'une position, ou l'évolution "
        "d'une thèse modifie les expositions et dépendances du portefeuille dans son ensemble.",
        include=[
            "Raisonne sur l'effet global d'un ajout, retrait ou changement de poids.",
            "Raisonne sur l'effet d'une évolution de thèse sur les expositions et dépendances du portefeuille.",
        ],
        exclude=[
            "Simple calcul mécanique d'un nouveau poids sans raisonnement système.",
        ],
        boundary_notes=[
            ("C3_C", "C3_C décrit la mécanique d'allocation ; C12_D décrit comment cette modification change le "
                     "système d'expositions, de dépendances et de thèses."),
        ],
    ),
]

_SPEC = {
    "taxonomy_schema_version": TAXONOMY_SCHEMA_VERSION,
    "version_key": VERSION_KEY,
    "global_mapping_rules": _GLOBAL_MAPPING_RULES,
    "competencies": _COMPETENCIES,
    "capabilities": _CAPABILITIES,
}


def taxonomy_v1_spec() -> dict:
    """Copie profonde indépendante de la SPEC V1 : muter le résultat ne
    modifie jamais la SPEC du module."""
    return copy.deepcopy(_SPEC)


# --------------------------------------------------------------------------
# Validation statique
# --------------------------------------------------------------------------

def _fail(message: str):
    raise TaxonomySpecError(message)


def _require_keys(value, keys: frozenset, path: str) -> None:
    if type(value) is not dict:
        _fail(f"{path} doit être un dict")
    if set(value) != keys:
        missing, extra = sorted(keys - set(value)), sorted(set(value) - keys, key=str)
        _fail(f"{path} : clés attendues {sorted(keys)} (manquantes {missing}, en trop {extra})")


def _require_text(value, path: str) -> None:
    """str exacte (une sous-classe est refusée), non vide après strip, sans
    NUL. Aucune normalisation : la chaîne est validée telle quelle."""
    if type(value) is not str or not value.strip():
        _fail(f"{path} doit être une chaîne non vide")
    if "\x00" in value:
        _fail(f"{path} : caractère NUL refusé")


def _require_text_list(value, path: str, *, allow_empty: bool) -> None:
    if type(value) is not list:
        _fail(f"{path} doit être une list")
    if not value and not allow_empty:
        _fail(f"{path} ne doit pas être vide")
    for index, item in enumerate(value):
        _require_text(item, f"{path}[{index}]")


def _require_list(value, path: str) -> list:
    if type(value) is not list:
        _fail(f"{path} doit être une list")
    return value


def _require_exact_codes(found: list, expected: tuple, path: str) -> None:
    duplicates = sorted({code for code in found if found.count(code) > 1}, key=str)
    if duplicates:
        _fail(f"{path} : codes en double {duplicates}")
    if len(found) != len(expected) or set(found) != set(expected):
        missing = [code for code in expected if code not in found]
        extra = sorted((code for code in found if code not in expected), key=str)
        _fail(f"{path} : exactement {len(expected)} codes attendus (manquants {missing}, en trop {extra})")


def _validate_guidance(guidance, path: str) -> None:
    _require_keys(guidance, GUIDANCE_KEYS, path)
    _require_text_list(guidance["include"], f"{path}.include", allow_empty=False)
    _require_text_list(guidance["exclude"], f"{path}.exclude", allow_empty=False)
    notes = _require_list(guidance["boundary_notes"], f"{path}.boundary_notes")
    for index, note in enumerate(notes):
        note_path = f"{path}.boundary_notes[{index}]"
        _require_keys(note, BOUNDARY_NOTE_KEYS, note_path)
        _require_text(note["against"], f"{note_path}.against")
        _require_text(note["rule"], f"{note_path}.rule")


def validate_spec(spec) -> None:
    """Validation statique stricte de la SPEC Oryx V1, sans aucun accès à
    la base ; lève TaxonomySpecError au premier écart. Vérifie l'identité V1
    (schéma 1, oryx-v1), les 8 règles R1..R8, les 12 compétences C1..C12,
    les 45 codes figés (ni manquant, ni en trop, ni doublon), la révision 1,
    la cohérence code / compétence, les textes non vides, et la structure
    EXACTE de mapping_guidance ({include, exclude, boundary_notes},
    boundary_notes = [{against, rule}]). Types JSON exacts : ni tuple, ni
    sous-classe de str, ni bool pour un entier."""
    _require_keys(spec, SPEC_KEYS, "spec")
    if type(spec["taxonomy_schema_version"]) is not int or spec["taxonomy_schema_version"] != TAXONOMY_SCHEMA_VERSION:
        _fail(f"taxonomy_schema_version : {TAXONOMY_SCHEMA_VERSION} attendu, reçu {spec['taxonomy_schema_version']!r}")
    if type(spec["version_key"]) is not str or spec["version_key"] != VERSION_KEY:
        _fail(f"version_key : {VERSION_KEY!r} attendu, reçu {spec['version_key']!r}")

    rules = _require_list(spec["global_mapping_rules"], "global_mapping_rules")
    for index, rule in enumerate(rules):
        path = f"global_mapping_rules[{index}]"
        _require_keys(rule, RULE_KEYS, path)
        for key in ("rule_code", "label", "rule"):
            _require_text(rule[key], f"{path}.{key}")
    _require_exact_codes([r["rule_code"] for r in rules], GLOBAL_RULE_CODES, "global_mapping_rules")

    competencies = _require_list(spec["competencies"], "competencies")
    for index, competency in enumerate(competencies):
        path = f"competencies[{index}]"
        _require_keys(competency, COMPETENCY_KEYS, path)
        for key in ("competency_code", "label", "central_question"):
            _require_text(competency[key], f"{path}.{key}")
    _require_exact_codes([c["competency_code"] for c in competencies], COMPETENCY_CODES, "competencies")

    capabilities = _require_list(spec["capabilities"], "capabilities")
    for index, capability in enumerate(capabilities):
        path = f"capabilities[{index}]"
        _require_keys(capability, CAPABILITY_KEYS, path)
        for key in ("capability_code", "competency_code", "label", "definition"):
            _require_text(capability[key], f"{path}.{key}")
        code = capability["capability_code"]
        path = f"capabilities[{code}]"
        if code not in CAPABILITY_CODES:
            _fail(f"{path} : code hors des 45 codes figés")
        revision = capability["semantic_revision"]
        if type(revision) is not int or revision != V1_SEMANTIC_REVISION:
            _fail(f"{path}.semantic_revision : {V1_SEMANTIC_REVISION} attendu pour V1, reçu {revision!r}")
        if capability["competency_code"] not in COMPETENCY_CODES:
            _fail(f"{path}.competency_code : hors de C1..C12")
        if code.split("_", 1)[0] != capability["competency_code"]:
            _fail(f"{path} : n'appartient pas à la compétence {capability['competency_code']}")
        _validate_guidance(capability["mapping_guidance"], f"{path}.mapping_guidance")
    _require_exact_codes([c["capability_code"] for c in capabilities], CAPABILITY_CODES, "capabilities")


# --------------------------------------------------------------------------
# Canonicalisation et fingerprint
# --------------------------------------------------------------------------

_RULE_CODE = re.compile(r"R([1-9][0-9]*)")
_COMPETENCY_CODE = re.compile(r"C([1-9][0-9]*)")
_CAPABILITY_CODE = re.compile(r"C([1-9][0-9]*)_([A-Z])")


def _natural_key(pattern, code, path: str) -> tuple:
    """Clé de tri naturelle (C2 < C10) ; un code non conforme est refusé
    plutôt que trié arbitrairement."""
    match = pattern.fullmatch(code) if type(code) is str else None
    if match is None:
        _fail(f"{path} : code {code!r} non ordonnable")
    return tuple(int(g) if g.isdigit() else g for g in match.groups())


def _strict_json(value, path: str):
    """Copie profonde d'une valeur strictement JSON (types exacts : ni
    tuple, ni set, ni sous-classe, ni NaN / infini, clés str sans NUL).
    Logique volontairement locale (ce module ne dépend pas du service T4-B)."""
    if value is None or type(value) in (bool, int, str):
        if type(value) is str and "\x00" in value:
            _fail(f"{path} : caractère NUL refusé")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail(f"{path} : nombre non fini refusé")
        return value
    if type(value) is list:
        return [_strict_json(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                _fail(f"{path} : clé {key!r} refusée (str sans NUL attendue)")
            result[key] = _strict_json(item, f"{path}.{key}")
        return result
    _fail(f"{path} : type {type(value).__name__} non JSON strict")


def canonical_payload(spec) -> dict:
    """Payload canonique : les cinq clés de premier niveau, les règles, les
    compétences et les capacités triées dans l'ordre naturel (jamais
    l'ordre accidentel de la source). Structure minimale exigée pour trier ;
    la validation métier V1 relève de validate_spec (le bootstrap appelle
    les deux). Rien d'autre (identifiant, horodatage, statut) n'y entre."""
    _require_keys(spec, SPEC_KEYS, "spec")
    payload = _strict_json(spec, "spec")
    for key, pattern, code_key in (("global_mapping_rules", _RULE_CODE, "rule_code"),
                                   ("competencies", _COMPETENCY_CODE, "competency_code"),
                                   ("capabilities", _CAPABILITY_CODE, "capability_code")):
        items = _require_list(payload[key], key)
        for index, item in enumerate(items):
            if type(item) is not dict or code_key not in item:
                _fail(f"{key}[{index}] : {code_key} manquant")
        payload[key] = sorted(items, key=lambda item: _natural_key(pattern, item[code_key], key))
    return payload


def canonical_json(spec) -> bytes:
    """JSON canonique UTF-8 : clés triées, séparateurs compacts, Unicode
    préservé (ensure_ascii=False), NaN refusé."""
    return json.dumps(canonical_payload(spec), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def compute_spec_fingerprint(spec) -> str:
    """SHA-256 hexadécimal minuscule (64 caractères) du JSON canonique."""
    return hashlib.sha256(canonical_json(spec)).hexdigest()


def load_taxonomy_v1() -> tuple[dict, str]:
    """(SPEC V1 validée, fingerprint) — point d'entrée du bootstrap, de la
    vérification et de l'activation. La SPEC est validée, son fingerprint
    recalculé puis comparé à EXPECTED_V1_FINGERPRINT : une SPEC modifiée
    sans décision explicite (constante figée) est refusée avant toute
    lecture ou écriture en base."""
    spec = taxonomy_v1_spec()
    validate_spec(spec)
    fingerprint = compute_spec_fingerprint(spec)
    if fingerprint != EXPECTED_V1_FINGERPRINT:
        _fail(f"fingerprint de la SPEC {fingerprint} différent de EXPECTED_V1_FINGERPRINT "
              f"{EXPECTED_V1_FINGERPRINT}")
    return spec, fingerprint
