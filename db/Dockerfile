FROM timescale/timescaledb@sha256:95a7997408e3295935e4afe8af8c09aae22bd51732131d1e7351c63d6198a71c AS production
COPY schema.sql /docker-entrypoint-initdb.d/001_schema.sql

FROM production AS demo
COPY demo/900_demo_seed.sql /docker-entrypoint-initdb.d/900_demo_seed.sql
