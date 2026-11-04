"""Sélection PURE de la méthode pédagogique d'un tour Décrypter F2
(R1 PR 9-C1-B) — module unique, partagé par la génération B2
(core.authz_f2.decryptage_generation) et par la revalidation de la
finalisation C1-B (core.authz_f2._decryptage_orm_bridge) : aucune règle
n'est dupliquée.

Règles EXACTES de DecryptageGenerationOrchestrator._prepare_prompt (R1 PR
9-B2), elles-mêmes recopiées de la route historique /decryptage :

1. AnalysisSession active -> construction_these forcée, base active_analysis ;
2. sinon, contexte conversationnel vide -> construction_these forcée, base
   initial_turn ;
3. sinon -> core.pedagogie_library.lookup_method(..., log=False) (jamais la
   question imprimée ni journalisée, audit B2 P1-01), base lookup ; une
   question vide est remplacée par le texte de repli historique fondé sur le
   nom de société (nom du marché B2 vérifié, sinon le ticker résolu).

Aucun appel fournisseur, aucune base, aucune connexion, aucune variable
d'environnement. N'importe ni le runner, ni les adaptateurs SQL, ni le pont
ORM, ni l'orchestrateur de génération.
"""
from dataclasses import dataclass

from core.authz_f2 import decryptage_candidate as dcand
from core.pedagogie_library import METHODES, lookup_method


def forced_construction_these() -> dict:
    """Méthode construction_these forcée (copie de
    api._force_construction_these_method, sans import de api) ; NOUVEAU dict."""
    method = METHODES[dcand.CONSTRUCTION_THESE]
    return {"method_id": dcand.CONSTRUCTION_THESE, "title": method["title"],
            "method_content": method["method_content"], "example_company": method["example_company"],
            "keywords_matched": []}


def company_name(market_name, ticker: str) -> str:
    """Nom présenté (historique : nom fourni, sinon le ticker)."""
    return market_name or ticker


@dataclass(frozen=True, slots=True)
class MethodSelection:
    method: object  # dict de la méthode (prompt) ou None ; jamais partagé (copie par appel)
    basis: str      # dcand.METHOD_BASES

    @property
    def method_id(self):
        return self.method["method_id"] if self.method else None


def select_method(*, has_active_session: bool, question: str, context: str, last_method_id,
                  market_name, ticker: str) -> MethodSelection:
    """Méthode et base d'un tour (règles 1 à 3 ci-dessus)."""
    if type(has_active_session) is not bool:
        raise TypeError("has_active_session : booléen attendu")
    if has_active_session:
        return MethodSelection(forced_construction_these(), dcand.BASIS_ACTIVE_ANALYSIS)
    if not context:
        return MethodSelection(forced_construction_these(), dcand.BASIS_INITIAL_TURN)
    lookup_text = question if question else f"analyser bilan états financiers {company_name(market_name, ticker)}"
    # log=False : sélection historique identique, sans jamais imprimer la question (audit P1-01).
    method = lookup_method(lookup_text, context=context, last_method_id=last_method_id, log=False)
    return MethodSelection(method, dcand.BASIS_LOOKUP)
