#!/usr/bin/env bash
# ===========================================================================
# DEPRECATED — DO NOT USE.
#
# This launcher disabled ESB and scalp and substituted the protected-hold
# primary rule. That is NOT what v36c-3 dry-live validated. Per operator
# instruction (2026-05-12 post-release-decision), the v36c-3 production path
# must mirror the EXACT dry-live system: Entry Snapshot Bank + scalp/no-hold
# bank/scratch + v33 route-aware accounting + QuoteManager + risk worker.
#
# Use instead:
#   start_v36c3_live_esb_smoke.sh
#
# That launcher keeps ESB ON, scalp ON, and depends on a live-equivalent
# Jito atomic buy+sell bundle (pgg2_live_esb_executor.py + Jito Block
# Engine). The protected-hold-only smoke is preserved here for historical
# reference but must not be invoked for v36c-3 production validation.
# ===========================================================================
echo "DEPRECATED: this launcher (start_v36c3_live_smoke.sh) is disabled."
echo "Use start_v36c3_live_esb_smoke.sh for the ESB live smoke."
echo "See RELEASE_NOTES_V36C3.md (v36c-3 live ESB section) for procedure."
exit 1
