"""An autonomous manager for a single Yahoo fantasy football team."""

__all__ = ["board", "config", "draft", "draft_ui", "lineup", "models", "names", "policy"]
# "auth" and "draft_sync" are deliberately excluded: both touch the network
# (requests / yahoo_fantasy_api) and must not be pulled in by a star-import.
