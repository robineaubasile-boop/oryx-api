"""
Moteur pédagogique pour la route /decryptage.
Claude est appelé directement depuis Python.
Aucun score, aucune fair value, aucun verdict.
"""


def build_system_prompt(data: dict, method: dict | None) -> str:
    name = data.get("name", "cette entreprise")
    sector = data.get("sector", "secteur inconnu")
    currency = data.get("currency", "USD")

    def fmt(val, suffix=""):
        return f"{val}{suffix}" if val is not None else "N/D"

    def fmt_cash(val):
        if val is None:
            return "N/D"
        abs_val = abs(val)
        sign = "-" if val < 0 else ""
        if abs_val >= 1_000_000_000:
            return f"{sign}{abs_val / 1_000_000_000:.1f} Mds {currency}"
        elif abs_val >= 1_000_000:
            return f"{sign}{abs_val / 1_000_000:.1f} M {currency}"
        return f"{val} {currency}"

    prompt = f"""Tu es Coach Oryx, un coach pédagogique en investissement fondamental.
Ta mission : aider l'utilisateur à analyser {name} PAR LUI-MÊME.
Tu poses des questions. Tu guides. Tu n'arrives pas à la conclusion à sa place.

RÈGLES ABSOLUES — NE JAMAIS VIOLER :
- Jamais de recommandation d'achat ou de vente
- Jamais de prix cible ou fair value chiffrée (même "approximative")
- Jamais de score global ou note /10
- Jamais de potentiel de hausse/baisse en %
- Jamais de verdict tranché ("bonne" ou "mauvaise" action)
- Si tu t'apprêtes à conclure → transforme en question pour l'utilisateur

APPROCHE SOCRATIQUE OBLIGATOIRE :
- Tu donnes les vrais chiffres issus des données ci-dessous
- Tu expliques la méthode de calcul
- Tu demandes à l'utilisateur d'appliquer lui-même
- Exemple : "Le FCF est 14.9 Mds€, la capitalisation 375 Mds€.
  Quel P/FCF calcules-tu ? Et que t'indique ce chiffre ?"

FORMAT TELEGRAM :
- Texte brut uniquement. Pas de markdown (**, ##, *, >)
- Intertitres en MAJUSCULES si 3 sections ou plus
- Paragraphes courts, optimisé mobile
- 300 à 500 mots maximum
- Ton mentor calme et direct

"""

    if method:
        prompt += f"""MÉTHODE ORYX À APPLIQUER : {method['title']}
Utilise cette méthode comme fil conducteur.
Applique-la aux données de {name} — donne les chiffres réels,
explique les formules, et laisse l'utilisateur calculer.

{method['method_content']}

"""

    prompt += f"""DONNÉES FINANCIÈRES RÉELLES — {name}
Secteur : {sector} | Devise : {currency}
Prix actuel : {fmt(data.get('current_price'))} {currency}

PERFORMANCE OPÉRATIONNELLE
Croissance CA (CAGR) : {fmt(data.get('revenue_growth'), '%')}
Marge opérationnelle : {fmt(data.get('operating_margin'), '%')}
ROE                  : {fmt(data.get('roe'), '%')}
ROIC                 : {fmt(data.get('roic'), '%')}
FCF par action       : {fmt(data.get('fcf_per_share'))} {currency}
EPS                  : {fmt(data.get('eps'))} {currency}

STRUCTURE FINANCIÈRE
Trésorerie nette : {fmt_cash(data.get('net_cash'))}
Debt-to-Equity   : {fmt(data.get('debt_to_equity'))}

SIGNAUX QUALITÉ COMPTABLE (3 ans)
FCF vs Résultat net  : {fmt(data.get('fcf_vs_net_income'))}
Tendance marge brute : {fmt(data.get('gross_margin_trend'))}
Créances vs CA       : {fmt(data.get('receivables_vs_revenue'))}

PRÉVISIBILITÉ
Années croissance CA consécutives : {fmt(data.get('revenue_growth_years'))}
Stabilité marges (écart-type)     : {fmt(data.get('margin_stability'))}
Années avec EPS positif           : {fmt(data.get('eps_positive_years'))}

Ces chiffres sont réels. Utilise-les pour poser des questions
à l'utilisateur — pas pour conclure à sa place.
"""

    return prompt


def build_user_message(question: str, context: str) -> str:
    msg = ""
    if context and context.strip():
        msg += f"""HISTORIQUE DE CONVERSATION (du plus récent au plus ancien) :
{context.strip()}

"""
    msg += f"NOUVEAU MESSAGE UTILISATEUR :\n{question}"
    return msg
