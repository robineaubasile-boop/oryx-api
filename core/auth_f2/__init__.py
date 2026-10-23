"""Services internes F2 (R1 PR 2) — DORMANTS.

Implémentation du contrat C.2.1 et de son annexe C.2.1-A : protocole
cryptographique F2-C2-V1, OTP Supabase (generateLink -> HMAC local ->
Resend -> verifyOtp), invitations, liaison e-mail, quotas anti-abus,
sessions Oryx canoniques et reprise du résultat.

Dormance : aucun module applicatif (api.py, workers, scripts, frontend)
n'importe ce paquet ; aucune route, aucun cookie, aucun appel fournisseur
réel. Les fournisseurs et le trousseau de clés sont injectés par
l'appelant ; les tests n'utilisent que des doubles et des clés fictives.
L'activation exige d'abord la fermeture des blocages listés dans
docs/f2_r1_pr2.md.
"""
