from .base import *

DEBUG = True
DATABASES["default"] = env.db(
    "DATABASE_URL",
    default="postgresql://postgres:postgres@pgbouncer:5433/aviation_safety",
)
