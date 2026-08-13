"""An autonomous manager for a single Sleeper fantasy football team."""

__all__ = ["board", "config", "draft", "draft_ui", "lineup", "models", "names", "policy"]
# "draft_sync" and "sleeper" are deliberately excluded: both touch the
# network (stdlib urllib) and must not be pulled in by a star-import.
