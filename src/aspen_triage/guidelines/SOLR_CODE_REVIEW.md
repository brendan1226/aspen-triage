# Aspen Discovery - SOLR Implementation Code Review

**Date:** 2026-03-31
**Branch:** solr-review (based on 26.05.00)
**Reviewer:** Claude (AI-assisted review)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Architecture Overview](#architecture-overview)
3. [Schema & Field Type Analysis](#schema--field-type-analysis)
4. [Query Patterns (PHP)](#query-patterns-php)
5. [Indexing & Document Updates (Java)](#indexing--document-updates-java)
6. [Infrastructure & Operations](#infrastructure--operations)
7. [Bugs Found](#bugs-found)
8. [Recommendations by Priority](#recommendations-by-priority)

---

## Executive Summary

Aspen Discovery runs **Apache Solr 8.11.2** in standalone mode with **8 cores** serving different content types (catalog, lists, events, series, genealogy, open archives, course reserves, website pages). The PHP frontend queries Solr via a custom `CurlWrapper` (no client library), while the Java backend indexes via `ConcurrentUpdateHttp2SolrClient`.

**Overall Assessment:** The implementation is functional and well-structured for a library discovery system. However, there are meaningful performance and maintainability improvements available — particularly around indexing throughput, cache tuning, query efficiency, and field storage optimization.

### Key Metrics

| Metric | Value |
|--------|-------|
| Solr Version | 8.11.2 (Lucene 8.11.2) |
| Number of Cores | 8 |
| Fields in main core (grouped_works_v2) | ~90+ |
| Field Types | ~24 |
| PHP Connector Classes | 9 |
| Java Indexer Classes | 7+ |
| Bugs Found | 1 confirmed |

---

## Architecture Overview

### Cores

| Core | Purpose |
|------|---------|
| `grouped_works_v2` | Primary catalog/bib search (largest, most complex) |
| `lists` | User-created lists |
| `events` | Event calendar entries |
| `series` | Book series |
| `course_reserves` | Course reserve materials |
| `genealogy` | Genealogy records |
| `open_archives` | OAI-PMH harvested content |
| `website_pages` | CMS/website content |

### Data Flow

```
[ILS / Data Sources]
        |
        v
[Java Indexers] --HTTP/2--> [Solr 8.11.2 (8 cores)]
                                    |
                                    v
                            [PHP Frontend] <--cURL/REST-- [Solr]
                                    |
                                    v
                              [End Users]
```

### Key Files

| Component | Path |
|-----------|------|
| Core configs | `data_dir_setup/solr7/*/conf/` |
| PHP connectors | `code/web/sys/SolrConnector/` |
| PHP searchers | `code/web/sys/SearchObject/` |
| Java indexers | `code/reindexer/src/org/aspen_discovery/reindexer/` |
| Solr distribution | `sites/default/solr-8.11.2/` |
| JVM config | `sites/default/solr-8.11.2/bin/solr.in.sh` |

---

## Schema & Field Type Analysis

### Field Types (grouped_works_v2)

The schema defines ~24 field types. Key observations:

| Field Type | Tokenizer | Filters | Use Case | Notes |
|------------|-----------|---------|----------|-------|
| `searchable_text` | ICU Tokenizer | WordDelimiter, ICU Folding, KeywordRepeat, Snowball Stemmer, RemoveDuplicates | General search | Well-configured for multilingual |
| `searchable_text_minimal_stem` | ICU Tokenizer | ICU Folding, KStem | Titles, subjects | Good — KStem is less aggressive |
| `searchable_text_unstemmed` | ICU Tokenizer | WordDelimiter, ICU Folding | Exact phrase matching | Appropriate for precision queries |
| `text-left` | ICU Tokenizer | EdgeNGram (1-25) | Typeahead/autocomplete | EdgeNGram range could be narrowed |
| `text-exact` | KeywordTokenizer | ICU Folding, Padding | Exact field matching | Fine |
| `callnumber-search` | Whitespace | ICU Folding, pattern replace | Call number lookups | Specialized, appropriate |
| `alphaOnlySort` | KeywordTokenizer | Lowercase, TrimFilter, pattern replace | Sort fields | Fine |

**Observation:** The text analysis chain is well-designed for library catalog search. ICU tokenization and folding provide good multilingual support. The multiple stemming variants (full, minimal, unstemmed) allow tuning relevance across different field types.

### Field Storage Optimization

Many facet fields have **both** `stored="true"` AND `docValues="true"`. For fields used only for faceting/sorting, `stored="true"` is redundant and wastes disk space — Solr can return docValues fields without storing them.

**Affected fields** (in `grouped_works_v2/conf/schema.xml`):

- `subject_facet`, `topic_facet`, `genre_facet` (lines 292-302)
- `geographic_facet`, `era` (lines 305-308)
- `awards_facet`, `literary_form`, `target_audience` (lines 310-315)
- `language`, `itype`, `mpaa_rating` (lines 319-329)
- `lexile_code`, `accelerated_reader_*` fields
- Various `*_facet` fields

**Estimated Impact:** For a catalog of 500K+ grouped works, removing unnecessary `stored` on ~20 facet fields could reduce index size by 5-15%.

### Dynamic Fields

Extensively used for per-library scoping:

- `available_copies_*`, `local_callnumber_*`, `local_time_since_added_*` — appropriate for multi-tenant library consortia
- `lib_boost_*` — per-library relevance boosting
- `scoping_details_*` — detailed availability per scope

**Note:** The dynamic field pattern is well-suited for Aspen's multi-library architecture, but each scope adds fields to every document, which grows index size linearly with the number of scopes.

### Copy Fields

~40 copy field rules feed:
- Suggestion fields (title/author/subject -> suggestions)
- Unstemmed variants for exact matching
- Spelling dictionary
- Facet fields from searchable fields

**Observation:** Copy fields are a significant contributor to index size. Each copy field essentially doubles the storage for that data. Review whether all unstemmed/proper variants are actually queried.

### Term Vectors

Enabled on: `subject_facet`, `topic_facet`, `genre_facet`, `awards_facet`, `era`

These support the MoreLikeThis handler but increase index size. Verify that MLT is actively used on all these fields; if not, disable term vectors on unused ones.

---

## Query Patterns (PHP)

### Query Construction

**File:** `code/web/sys/SolrConnector/Solr.php` (~2,220 lines)

Queries are built via string concatenation with YAML-configured field specifications. This is a **custom REST client** — no Solr client library (like Solarium) is used.

```php
// Line 1202 - Query assembly
$options['q'] = "({$handler}:{$query})";

// Line 325 - Filter query assembly
$filterQuery[] = "$fieldPrefix$field:$value";
```

**Security:** Input validation exists via `validateInput()` and `capitalizeBooleans()`. Values are quoted and URL-encoded. No critical injection vulnerabilities found, though there's no centralized Solr character escaping utility.

### Search Handler Configuration

- **Simple searches:** Use DisMax via YAML-configured `qf` (query fields) with boost weights
- **Advanced searches:** Fall back to standard Lucene syntax
- **MoreLikeThis:** Dedicated `/mlt` handler with field-specific boosting
- **Suggestions:** Six suggesters using `BlendedInfixLookupFactory` and `AnalyzingInfixLookupFactory`

### Faceting

```php
// Line 1277-1281 in Solr.php
$options['facet.method'] = 'fcs';      // Fast Count Searcher
$options['facet.threads'] = 25;
```

- Uses `facet.method=fcs` — this is generally good but consider `enum` method for low-cardinality fields
- `facet.threads=25` is high; should match available CPU cores, not exceed them
- Multi-select faceting properly implemented with `{!tag=X}` / `{!ex=X}` syntax

### Field List (fl) Issues

**File:** `code/web/sys/SearchObject/GroupedWorkSearcher.php` (line 8)

```php
public static string $fields_to_return = 'auth_author2,author2-role,id,mpaa_rating,title_display,...';
// 60+ fields requested by default
```

**Problem:** Requesting 60+ fields per result increases network overhead and Solr processing time. Different views (search results list vs. detail page) need different field sets.

**Recommendation:** Create view-specific field lists:
- List view: ~15-20 fields (id, title_display, author_display, format, cover image fields)
- Detail view: full field set
- API view: minimal required fields

### Batch Record Retrieval

```php
// Lines 397-447 in Solr.php - Fixed batch size of 40
$batchSize = 40;
while (true) {
    $tmpIds = array_slice($ids, $startIndex, $batchSize);
    $idString = implode(' OR ', $tmpIds);
    $options = ['q' => "id:($idString)"];
    // Sequential GET requests
}
```

**Issues:**
1. Fixed batch size not optimized for request payload size
2. Uses GET (URL length concerns) instead of POST
3. Sequential requests — no parallel fetching
4. For 200 IDs = 5 sequential HTTP round trips

### Caching Gap

Only Solr ping results are cached (via memcache). Search results, facet counts, and suggestions are **never cached** at the application level.

**Impact:** Identical searches by different users always hit Solr. Popular/common searches (empty searches, browse pages) could benefit significantly from short-lived application-level caching.

---

## Indexing & Document Updates (Java)

### Client Configuration

All indexers use the same pattern:

```java
// GroupedWorkIndexer.java line 399-402
updateServer = new ConcurrentUpdateHttp2SolrClient.Builder(solrUrl, http2Client)
    .withThreadCount(1)
    .withQueueSize(25)
    .build();
```

**Issues:**

| Setting | Current | Recommended | Rationale |
|---------|---------|-------------|-----------|
| Thread count | 1 | 2-4 | Enable parallel document sending |
| Queue size | 25 | 100-500 | Reduce flush frequency, improve throughput |
| Connection pooling | Default | Explicit config | Control max connections |

### Document Submission Pattern

```java
// Line 1262 - One document at a time
UpdateResponse response = updateServer.add(inputDocument);
```

Documents are added individually. While `ConcurrentUpdateHttp2SolrClient` has an internal queue that provides implicit batching, explicit batch adds via `addDocuments(Collection<SolrInputDocument>)` would be more efficient.

### Commit Strategy

```java
// Commit pattern used throughout
updateServer.commit(false, false, true);  // waitFlush=false, waitSearcher=false, expungeDeletes=true
```

**Commit intervals** (configurable from database):
- Deletion commit interval: 1,000 records (default)
- Index commit interval: 10,000 records (default)

**Issues:**
1. `expungeDeletes=true` on every commit is expensive — it forces segment merging to physically remove deleted documents
2. Hard commits during bulk indexing open/close index writers repeatedly
3. Better pattern: soft commits during indexing, single hard commit with expungeDeletes at end

### Full Reindex Process

1. `deleteByQuery("recordtype:grouped_work")` — clears all catalog records
2. Iterates through ALL grouped works from database
3. Commits every 10,000 records
4. Final commit at end

**Concern:** The initial `deleteByQuery` without an immediate commit means deleted and new documents coexist during reindex. This is actually fine since `openSearcher=false` on autoCommit means users see the old index until the explicit commit.

### Document Complexity

Each `GroupedWorkSolr2` document builds 100+ fields with extensive use of:
- `HashSet<String>` for multi-valued fields
- `HashMap<Integer, Set<String>>` for scoped fields
- Deep clone operations on all collections (`AbstractGroupedWorkSolr` lines 138-215)

**Memory concern:** For large catalogs (1M+ records), the per-document memory allocation and GC pressure from cloning HashSets/HashMaps could be significant. Consider object pooling or reuse patterns.

### Error Handling

```java
// Line 1263-1266 - Only logs errors, no retry
if (response.getStatus() != 0) {
    logger.error("Error adding document...");
}
```

No retry mechanism for failed document adds. Network blips or temporary Solr issues will silently drop documents.

---

## Infrastructure & Operations

### Deployment

- **Mode:** Standalone (SolrCloud config exists but not actively used)
- **Service management:** systemd/init.d with PHP-based start/stop (`SolrUtils.php`)
- **Docker support:** `solr:8.11.2` image with Lando dev environment

### JVM Configuration

**File:** `sites/default/solr-8.11.2/bin/solr.in.sh`

| Setting | Default | Notes |
|---------|---------|-------|
| Heap | 512MB | Low for production catalogs |
| GC | CMS (commented) | Should migrate to G1GC for Solr 8+ |
| OOM handling | Kill process | Via `oom_solr.sh` |

**System limits** (`install/solr_limits.conf`):
- File descriptors: 65,000
- Process limit: 65,000

### Cache Configuration

**File:** `data_dir_setup/solr7/grouped_works_v2/conf/solrconfig.xml` (lines 402-426)

| Cache | Size | autowarmCount | Notes |
|-------|------|---------------|-------|
| filterCache | 512 | 0 | Too small for complex faceted search |
| queryResultCache | 512 | 0 | No warming = cold restarts |
| documentCache | 512 | 0 | May be undersized |
| perSegFilter | 10 | 10 | Fine |

**Key issue:** `autowarmCount=0` means after every commit that opens a new searcher, all caches start empty. This causes a "cold cache storm" where the first users after a commit experience slow queries.

### Security

- Basic auth support available (not enabled by default)
- SSL/HTTPS configurable via environment
- No IP restrictions by default
- ZooKeeper ACL support available

### Monitoring

- `checkSolr.php` cron job verifies Solr is running
- `checkSolrForDeletedWorks.php` for index maintenance
- Ping endpoint cached for 5 seconds
- **No metrics collection** (JMX, Prometheus, etc.)
- **No alerting** on query latency, cache hit rates, or index errors

### Backup

- Application-level backup scripts exist (`backupAspen.php`)
- No Solr-specific snapshot/backup automation
- Transaction logs enabled for durability

---

## Bugs Found

### BUG: Typo in Genealogy Schema

**File:** `data_dir_setup/solr7/genealogy/conf/schema.xml`, line 94
**Field:** `deathDate`
**Issue:** `stored="trues"` should be `stored="true"`

```xml
<!-- Current (broken) -->
<field name="deathDate" type="date" indexed="true" stored="trues" />

<!-- Fix -->
<field name="deathDate" type="date" indexed="true" stored="true" />
```

**Impact:** Solr may ignore the invalid attribute or throw a parsing error depending on version. The `deathDate` field may not be stored/returned in results.

---

## Recommendations by Priority

### Critical (Fix Now)

| # | Issue | File | Impact |
|---|-------|------|--------|
| 1 | Fix `stored="trues"` typo | `genealogy/conf/schema.xml:94` | Genealogy death dates may not display |

### High Priority (Significant Performance Impact)

| # | Issue | Current | Recommended | Impact |
|---|-------|---------|-------------|--------|
| 2 | Increase Solr cache sizes | 512 entries, 0 autowarm | 2048+ entries, autowarmCount=64-128 | Faster queries after commits, fewer cold cache hits |
| 3 | View-specific field lists | 60+ fields always returned | 15-20 for list view, full for detail | Reduced network I/O and Solr processing |
| 4 | Stop using `expungeDeletes=true` on every commit | Every commit merges segments | Only on final commit or scheduled optimize | Dramatically faster commits during indexing |
| 5 | Increase indexer queue size and thread count | queue=25, threads=1 | queue=200, threads=2-4 | Higher indexing throughput |
| 6 | Add application-level search result caching | No caching | Cache popular queries for 60-300 seconds | Reduced Solr load, faster responses |
| 7 | Right-size `facet.threads` | 25 | Match CPU core count (4-8 typical) | Avoid thread contention |

### Medium Priority (Optimization)

| # | Issue | Details |
|---|-------|---------|
| 8 | Remove `stored="true"` from facet-only fields | ~20 fields have unnecessary storage; saves 5-15% index size |
| 9 | Use POST for batch record retrieval | GET with large ID lists risks URL length issues |
| 10 | Add retry logic to Java indexers | Failed document adds are silently dropped |
| 11 | Use `facet.method=enum` for low-cardinality fields | Fields like `language`, `format`, `literary_form` with <50 values benefit from enum method |
| 12 | Increase JVM heap for production | 512MB default is low; 2-4GB recommended for catalogs >500K records |
| 13 | Switch from CMS to G1GC | CMS is deprecated; G1GC handles large heaps better with lower pause times |
| 14 | Review copy field necessity | ~40 copy field rules; each increases index size. Verify all variants are queried. |

### Low Priority (Maintenance & Future-Proofing)

| # | Issue | Details |
|---|-------|---------|
| 15 | Add Solr monitoring/metrics | No JMX or Prometheus metrics; no alerting on cache hit rates or query latency |
| 16 | Add centralized Solr query escaping utility | No consistent escaping for special characters in query values |
| 17 | Prepare for Solr 9.x migration | Migrate to CaffeineCache, review deprecated features. Config variants already exist. |
| 18 | Audit term vectors usage | Enabled on 5 facet fields; confirm MoreLikeThis uses all of them |
| 19 | Consider Solr backup automation | No Solr-specific snapshot/restore scripts |
| 20 | Document dynamic field growth pattern | Index size grows linearly with number of library scopes; plan for scaling |

---

## Quick Wins Checklist

These changes require minimal effort but provide measurable improvement:

- [ ] Fix genealogy `stored="trues"` typo
- [ ] Increase cache sizes to 2048 and set `autowarmCount=64`
- [ ] Change `expungeDeletes` to `false` on intermediate commits
- [ ] Increase indexer `queueSize` from 25 to 200
- [ ] Reduce default `facet.threads` from 25 to match CPU cores
- [ ] Increase JVM heap from 512MB to 2-4GB in production

---

## Appendix: Core File Reference

```
data_dir_setup/solr7/
  solr.xml                           # Cluster configuration
  grouped_works_v2/conf/
    schema.xml                       # Main catalog schema (~450 lines)
    solrconfig.xml                   # Main catalog config (~1400 lines)
  lists/conf/schema.xml              # Lists schema
  events/conf/schema.xml             # Events schema
  series/conf/schema.xml             # Series schema
  course_reserves/conf/schema.xml    # Course reserves schema
  genealogy/conf/schema.xml          # Genealogy schema (has bug)
  open_archives/conf/schema.xml      # Open archives schema
  website_pages/conf/schema.xml      # Website pages schema

code/web/sys/SolrConnector/
  Solr.php                           # Base connector (~2220 lines)
  GroupedWorksSolrConnector.php      # Catalog queries
  GroupedWorksSolrConnector2.php     # Catalog queries (v2)
  ListsSolrConnector.php             # Lists queries
  EventsSolrConnector.php            # Events queries

code/web/sys/SearchObject/
  SolrSearcher.php                   # Search orchestrator (~948 lines)
  GroupedWorkSearcher.php            # Catalog search

code/reindexer/src/org/aspen_discovery/reindexer/
  GroupedWorkIndexer.java            # Main indexer
  GroupedReindexMain.java            # Entry point
  GroupedWorkSolr2.java              # Document builder
  AbstractGroupedWorkSolr.java       # Base document fields
```
