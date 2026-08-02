# ============================================================================
# careeragent-jobs - Client Package
# Maintainer: William McKeon
# ============================================================================
#
# Outbound HTTP clients careeragent-jobs' worker uses to reach the leaf services
# it drives. One module per upstream service, named by the service called:
#
#   client/review.py    — POST /review-batch to careeragent-review (the work)
#   client/sessions.py  — POST /conversations/{id}/inject to careeragent-sessions
#                         (deliver the finished result into the conversation)
#
# PYTHONPATH inside the container is /app/src (per Dockerfile), so these are
# importable as `from client.review import ReviewClient`. The worker calls leaf
# services DIRECTLY — there is no agent loop and no model in this service.
# ============================================================================
