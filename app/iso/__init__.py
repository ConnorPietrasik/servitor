"""Isolation core: 16-slot uid pool, per-session overlay mounts, chown
handoff, setuid spawn, owner-based sweep, the single release() cleanup
path, and the versioned config swap. The app runs privileged (root);
the agent never is.
"""
