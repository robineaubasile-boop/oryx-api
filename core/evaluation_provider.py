"""Fournisseur LLM du worker d'évaluation R1-D1 (Anthropic Messages API).

api.py appelle le SDK anthropic directement (aucune abstraction partagée
n'existe dans le repo) et ne peut pas être importé par un worker sans
FastAPI : ce module en est l'équivalent minimal, réutilisant la même
bibliothèque et la même clé (ANTHROPIC_API_KEY).

Un SEUL model_id pour D1B et D1D (persisté dans ObservationEvaluationRun et
inclus dans l'input_fingerprint). Paramètres explicites, couverts par
PROMPT_SPEC_VERSION / EVALUATOR_VERSION :

- max_tokens = MAX_TOKENS (requête non streamée) ;
- timeout = ORYX_EVALUATION_LLM_TIMEOUT_SECONDS, strictement inférieur à la
  durée de lease (marge validée par la configuration du worker) ;
- max_retries = 0 : aucun retry implicite du SDK (un appel = une tentative,
  le temps d'un appel reste borné par le timeout, donc par la lease) ;
- ni température ni configuration de raisonnement : refusées ou fixées
  par les modèles récents ; la reproductibilité est portée par les
  versions, pas par l'échantillonnage. Aucune sortie structurée imposée par
  l'API (non garantie sur tous les modèles) : le serveur valide strictement
  le JSON (D1B / D1D).

Seul le texte des blocs "text" est retourné ; stop_reason différent de
end_turn (refus, troncature) => ProviderError. Aucun contenu de prompt ni de
réponse n'est journalisé ni placé dans un message d'exception.
"""
import anthropic

from core.evaluation_runtime import ProviderError, ProviderTimeout
from core.local_evaluator import EvaluatorRequest

MAX_TOKENS = 16000


class AnthropicEvaluationProvider:
    def __init__(self, *, model_id: str, timeout_seconds: int, api_key: str):
        self.model_id = model_id
        self._client = anthropic.Anthropic(api_key=api_key, timeout=float(timeout_seconds), max_retries=0)

    def complete(self, request: EvaluatorRequest) -> str:
        try:
            response = self._client.messages.create(
                model=self.model_id,
                max_tokens=MAX_TOKENS,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
            )
        except anthropic.APITimeoutError:
            raise ProviderTimeout("délai fournisseur dépassé") from None
        except anthropic.APIError as exc:
            raise ProviderError(f"erreur fournisseur {type(exc).__name__}") from None
        if response.stop_reason != "end_turn":
            raise ProviderError(f"stop_reason {response.stop_reason!r}")
        return "".join(block.text for block in response.content if block.type == "text")
