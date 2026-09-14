# PRD section 6.2 asks for "PostgreSQL 16 + PostGIS + pgvector" in one store:
# geo queries and embedding search side by side. No published image carries
# both - postgis/postgis has no pgvector, pgvector/pgvector has no PostGIS - so
# we add pgvector to the PostGIS image from the same PGDG apt repo the base
# image already trusts.
FROM postgis/postgis:16-3.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
