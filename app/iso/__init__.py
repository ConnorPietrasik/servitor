"""Isolation core (PLAN §5): 16-slot uid pool, per-session overlay mounts,
chown-to-slot handoff, setuid spawn (asserts the child's uid), owner-based
sweep walk, the single release() cleanup path, and the versioned config swap.
The app runs privileged (root); the agent never is.
"""
