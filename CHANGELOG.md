## v1.2.5 (2026-06-25)

### Fix

- **gui**: fix the concurrent download issue add workers to gui

## v1.2.4 (2026-06-25)

### Fix

- **download**: fix the concurrency on osmsg

## v1.2.3 (2026-06-24)

### Fix

- **gui**: windows

## v1.2.2 (2026-06-24)

### Fix

- **gui**: fixes gui for windows

## v1.2.1 (2026-06-24)

### Fix

- **chore**: cleanup and adds progress bar in the hf download

## v1.2.0 (2026-06-24)

### Feat

- **hisotry**: add osm hisotry for the stats

### Fix

- **build**: add build for the multiprocessing
- **precommit**: fixes precommit issue in conda receipe

## v1.1.2 (2026-06-04)

### Fix

- **replication**: fix last sequence on update missing stats
- **update**: fix hte update bug on tick

## v1.1.1 (2026-05-21)

### Fix

- **bug**: replication timestamp
- **osmsg**: update service configuration for docker compose
- **osmsg**: resolved markdown stats bug

## v1.1.0 (2026-05-08)

### Feat

- **infra**: adds infra docker compose for hosting osmsg

### Fix

- **test**: fix test cases on api
- **health**: fix health endpoint to include last_ts and updated_at
- **padding**: fix changeset pad
- **stats**: fix stats inconsistency on null
- **url**: respect url when it is passed for country
- **changeset**: null bug on bbox when newer one appears
- **ci**: fixes spatial extension loading bug
- **validation**: pydantic arg validation and docs with swagger
- **test**: don't wait for fetch state to be there
- **url**: api url arg default start end
- **health**: patch health endpoint to include the last sequence and updated at
- **docker**: caddy
- **docker**: resource limit in docker compose
- **docker**: docker compose prod cluster
- **caddy**: adds caddy server and fix for the api rendering on 80 port
- **schema**: fixes shcmea being in multiple pieces , added test case to catch the change
- **pipeline**: Replace hardcoded "processing" label with stage-specific descriptions

### Refactor

- **alltags**: refactors all tags and schema

## v1.0.3 (2026-04-28)

### Perf

- **url**: auto switch the replication url base don the input span

## v1.0.2 (2026-04-28)

### Fix

- **precommit**: add lock to precommit hooks
- **license**: fix license text on build

## v1.0.1 (2026-04-28)

### Fix

- **lock**: uv lock

## v1.0.0 (2026-04-28)

### Fix

- **docker**: fixes docker images , replaced  slim image with the distroless
- **ci**: fix lib creds on ci
- **data**: fix bug on data loss due to window changeset open
- **stat**: completeness test cases
- **stream**: fixes live streaming of the compressed osm files
- **test**: fixes test case strip issue
- **Dockerfile**: version upgrade in stage 1 - missed that one in the last commit
- **test_app.yml**: I had to remove "" from the python version number and change the number to python3.x

### Refactor

- **osmsg**: Updated the processing with this approach: Workers → write Parquet (independent) → final DuckDB merge.
- **osmsg**: Data type validation with pydantic models and multi-process implementations for processing of files
- **build.yml-test_app.yml**: remove uneeded installs in test_app.yml and let uv set up python in both test_app.yml and build.yml

### Perf

- **chore**: housekeeping removing dead links

## v0.3.0 (2024-08-26)

### Feat

- **toml-installation**: removes unnecessary codes adds toml installation with python build

### Fix

- **append-upgrade**: upgrade pandas .append

## v0.2.5 (2024-01-08)

### Fix

- build fix

## v0.2.4 (2024-01-08)

### Fix

- include meta lib info

## v0.2.3 (2024-01-08)

### Fix

- fixes bug on filepath

## v0.2.2 (2023-12-12)

### Fix

- Closes memory issue

## v0.2.1 (2023-12-07)

### Fix

- retry_max_url_attempt

## v0.2.0 (2023-10-30)

### Feat

- intorduces key value stats feature for all the stats type
- Turkey Data and Frontend Integration

## v0.1.33 (2023-07-10)

## v0.1.32 (2023-07-10)

### Perf

- **removing-files-on-temp**: temp file removal

## v0.1.31 (2023-07-07)

## v0.1.30 (2023-06-11)

## 0.1.27 (2023-03-21)

## 0.1.26 (2023-03-19)

## 0.1.25 (2023-03-18)

## 0.1.20 (2023-03-12)

## 0.1.19 (2023-03-08)

## 0.1.18 (2023-03-06)

## 0.1.17 (2023-03-05)

## 0.1.16 (2023-03-05)

## 0.1.14 (2023-03-04)

## 0.1.12 (2023-03-02)

## 0.1.10 (2023-03-01)

## 0.1.9 (2023-03-01)

## 1.0.8 (2023-02-28)

## 0.1.6 (2023-02-25)

## 1.0.5 (2023-02-25)

## 0.1.4 (2023-02-25)

## 0.1.3 (2023-02-22)

## 0.1.2 (2023-02-20)

## v0.1.1-alpha (2023-02-16)

## 0.1.1 (2023-02-16)

## 0.1.0 (2023-02-16)

## 0.0.31 (2023-02-16)

## 0.0.30 (2023-02-16)

## 0.0.29 (2023-02-02)

## 0.0.16 (2023-01-31)

## 0.0.11 (2023-01-28)

## 0.0.10 (2023-01-25)

## 0.0.9 (2023-01-23)

## 0.0.8 (2023-01-20)

## 0.0.7 (2023-01-13)

## 0.0.6 (2023-01-12)
