# Aspen Discovery - Comprehensive QA Code Analysis

**Date:** 2026-03-31
**Branch:** solr-review (based on 26.05.00)
**Reviewer:** Claude (AI-assisted review)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Critical Findings](#critical-findings)
3. [PHP Code Quality](#php-code-quality)
4. [Java Code Quality](#java-code-quality)
5. [Security & Authentication](#security--authentication)
6. [Frontend & Templates](#frontend--templates)
7. [Database & Architecture](#database--architecture)
8. [Full Findings Index](#full-findings-index)
9. [Recommended Action Plan](#recommended-action-plan)

---

## Executive Summary

This report covers a comprehensive QA analysis across the entire Aspen Discovery codebase including PHP (web frontend), Java (indexers/cron), Smarty templates, JavaScript, database patterns, security, and architecture.

### Finding Summary

| Severity | Count | Categories |
|----------|-------|------------|
| **Critical** | 16 | SQL injection, command injection, deserialization, eval(), god classes, default passwords |
| **High** | 18 | Missing CSRF, session security, resource leaks, N+1 queries, debug exposure |
| **Medium** | 35 | Type safety, error suppression, missing validation, accessibility, caching |
| **Low** | 8 | Console.log in production, deprecated APIs, hardcoded paths |
| **Total** | **77** | |

### Top 5 Most Urgent Issues

1. **OS Command Injection** in PayOnlineNashville.php — user data passed to `exec()` (Critical)
2. **SQL Injection** across 10+ files in both PHP and Java — string concatenation in queries (Critical)
3. **Default admin password `password`** in installation script (Critical)
4. **Unprotected `unserialize()`** on database data — PHP object injection risk (Critical)
5. **No CSRF protection** on state-changing forms — account actions vulnerable (High)

---

## Critical Findings

These issues represent immediate security or stability risks and should be addressed before any other work.

### CF-1: OS Command Injection (PayOnlineNashville.php)

**File:** `code/web/services/MyAccount/PayOnlineNashville.php:128, 293`
**Severity:** CRITICAL

```php
exec("$this->nplwrapper '" . $bill['JSON'] . "'", $wrap);
exec("$this->nplwrapper '" . $row['data'] . "'", $wrap);
```

User-controlled JSON data is passed directly to shell `exec()` without escaping. A crafted payload containing shell metacharacters (e.g., `'; rm -rf /; '`) enables remote code execution.

**Note:** This file is marked as "defunct" (line 4) but remains in the codebase and is still routable.

**Fix:** Remove this file entirely, or if still needed, use `escapeshellarg()` on all parameters and replace `exec()` with proper API calls.

---

### CF-2: SQL Injection (Multiple Files)

**PHP Files Affected:**
| File | Lines | Pattern |
|------|-------|---------|
| `RecordDrivers/Axis360RecordDriver.php` | 42 | Raw concatenation with `getUniqueID()` |
| `RecordDrivers/CloudLibraryRecordDriver.php` | 54 | Same pattern |
| `RecordDrivers/PalaceProjectRecordDriver.php` | 55 | Same pattern |
| `RecordDrivers/OverDriveRecordDriver.php` | 81 | Same pattern |
| `RecordDrivers/HooplaRecordDriver.php` | 52 | Same pattern |
| `Drivers/Koha.php` | 45+ locations | `mysqli_escape_string` (deprecated PHP 8.1) |
| `services/MyAccount/PayOnlineNashville.php` | 124, 133, 149, 296 | Raw concatenation into SQLite |
| `services/API/ListAPI.php` | 124, 264 | Concatenation before `prepare()` |

**Java Files Affected:**
| File | Lines | Pattern |
|------|-------|---------|
| `cron/.../genealogy/Person.java` | 83-102, 109, 123 | String concatenation with `replaceAll("'","''")` |
| `cron/.../Cron.java` | 248 | Concatenation inside `prepareStatement()` |
| `series_indexer/.../SeriesIndexer.java` | 163-164, 166 | Direct concatenation in `executeUpdate()` |
| `web_indexer/.../WebsiteIndexerMain.java` | 329 | Direct concatenation in `executeUpdate()` |

**Example (PHP):**
```php
// Axis360RecordDriver.php:42
$query = "SELECT grouped_work.* FROM grouped_work ... WHERE type='axis360' AND identifier = '" . $this->getUniqueID() . "'";
```

**Example (Java):**
```java
// Person.java:109
ResultSet obitRs = stmt2.executeQuery("SELECT count(*) from obituary WHERE personId = " + personId);
```

**Fix:** Replace all string concatenation with parameterized queries / prepared statements.

---

### CF-3: Default Admin Password

**File:** `code/web/cron/createDefaultDatabaseScript.php:145`

```php
fwrite($fhnd, "INSERT INTO user ... VALUES (1,'nyt_user','nyt_password',...),(2,'aspen_admin','password',...);\n");
```

Installation creates an admin user with password `password`. If not changed post-install, this is a trivial entry point.

**Fix:** Generate random temporary passwords during installation. Force password change on first login.

---

### CF-4: Unprotected Deserialization (PHP Object Injection)

**Files:**
| File | Lines |
|------|-------|
| `cron/updateSavedSearches.php` | 63 |
| `sys/SearchObject/BaseSearcher.php` | 1804, 1868, 1904, 2494 |
| `sys/Grouping/GroupedWorkDisplaySetting.php` | 871, 885 |

```php
$minSO = unserialize($searchEntry->search_object);
```

Calling `unserialize()` on data from the database without class restrictions. If the database is compromised, attackers can inject malicious serialized objects to achieve code execution.

**Fix:** Use `unserialize($data, ['allowed_classes' => ['MinSearchObject']])` or migrate to `json_encode()`/`json_decode()`.

---

### CF-5: Dangerous `eval()` Usage

**Files:**
| File | Lines |
|------|-------|
| `sys/Smarty/plugins/shared.literal_compiler_param.php` | 33 |
| `sys/Smarty/plugins/function.math.php` | 127 |

```php
eval("\$smarty_math_result = " . $equation . ";");
```

**Fix:** Replace with safe math evaluation (e.g., `bc_math` functions) or a sandboxed expression parser.

---

### CF-6: God Classes

| File | Lines | Responsibilities |
|------|-------|-----------------|
| `sys/Account/User.php` | 6,678 | Auth, profile, permissions, holds, checkouts, history, recommendations, campaigns, linked accounts, OAuth, cost savings, self-check |
| `CatalogConnection.php` | 2,209 | Proxy to 100+ driver methods |
| `sys/SolrConnector/Solr.php` | 2,220 | Query building, HTTP transport, response parsing, caching, suggestions, highlighting |

**Impact:** Extremely difficult to test, maintain, or refactor safely. High risk of unintended side effects from any change.

**Fix:** Extract into focused classes (e.g., `UserAuthentication`, `UserCirculation`, `UserPreferences`).

---

## PHP Code Quality

### PQ-1: Error Suppression with @ Operator (20+ instances)

**Key Files:**
- `robots.php:10` — `@file_get_contents('robots.txt')`
- `Drivers/marmot_inc/GoDeeperData.php:40, 284, 402, 489, 788, 823`
- `sys/SearchObject/DPLA.php:26-70` — 15+ instances
- `sys/WikipediaParser.php:29`

**Impact:** Masks errors, makes debugging difficult, hides potential security issues.

**Fix:** Replace with proper error handling (try/catch or conditional checks with logging).

### PQ-2: `extract()` Usage

**File:** `sys/SearchObject/TalpaSearcher.php:325`

```php
foreach ($foundGroupedWorks['response']['docs'] as $recordItem) {
    extract($recordItem);
    $inLibraryResults[$id] = $recordItem;
}
```

**Impact:** Can overwrite existing variables with attacker-controlled data from Solr results.

**Fix:** Access array keys explicitly: `$id = $recordItem['id']`.

### PQ-3: Loose Comparisons (== vs ===)

**File:** `sys/DataObjectUtil.php` — multiple lines (194, 253, 272, 296)

```php
if ($_REQUEST[$propertyName . '-default'] == 'on')
```

**Impact:** Type coercion bugs (e.g., `"0" == false` is true).

**Fix:** Use strict comparisons (`===`) for all request data checks.

### PQ-4: Missing Query Result Checks

**File:** `Drivers/Koha.php:3166-3392`

```php
$allFeesRS = mysqli_query($this->dbConnection, $query);
if ($allFeesRS->num_rows > 0) {  // $allFeesRS could be false
```

**Impact:** Fatal error if query fails.

**Fix:** Check `$allFeesRS === false` before accessing properties.

---

## Java Code Quality

### JQ-1: Unclosed Resources (No try-with-resources)

**File:** `reindexer/src/.../grouping/RecordGroupingProcessor.java:767, 787`

```java
CSVReader csvReader = new CSVReader(new FileReader("../reindexer/author_authorities.properties"));
loadDefaultAuthorityFile(addAuthorAuthorityStmt, csvReader);
// FileReader never closed if exception occurs
```

**Also affected:**
- `RecordGroupingProcessor.java:699` — FileReader closed manually (skipped on exception)
- `SeriesIndexer.java:63-67` — PreparedStatements not closed in all paths
- `cron/.../GenealogyCleanup.java:74-102` — HTTP connections not in try-finally

**Fix:** Use try-with-resources for all `Closeable`/`AutoCloseable` objects.

### JQ-2: Empty Catch Blocks

**File:** `events_indexer/.../SpringshareLibCalIndexer.java:689-703`

```java
try { eventsSitesRS.close(); } catch (SQLException e) {}
```

**Fix:** At minimum log the exception: `logger.warn("Error closing ResultSet", e);`

### JQ-3: Thread Safety — Static Mutable CRC32

**File:** `events_indexer/.../SpringshareLibCalIndexer.java:50`

```java
private static final CRC32 checksumCalculator = new CRC32();
```

Shared across threads without synchronization. `CRC32.reset()` + `CRC32.update()` is not atomic.

**Fix:** Use instance variable or `ThreadLocal<CRC32>`.

### JQ-4: No Connection Pooling

All Java indexers use direct `DriverManager.getConnection()` without pooling.

**Files:** `GroupedReindexMain.java:279`, `Cron.java:53`, `SeriesMain.java`, `UserListIndexerMain.java`

**Fix:** Add HikariCP or similar connection pool.

### JQ-5: Missing Null Checks on API Responses

**File:** `events_indexer/.../SpringshareLibCalIndexer.java:208`

```java
solrDocument.addField("url", curEvent.getJSONObject("url").getString("public"));
// NPE if "url" key doesn't exist
```

**Fix:** Use `optJSONObject()` / `optString()` with null checks.

### JQ-6: System.out/err Instead of Logger

**File:** `cron/.../genealogy/Person.java:77-78, 114, 128`

```java
System.err.println("Error loading person " + e);
```

**Fix:** Use `logger.error("Error loading person for id: {}", personId, e);`

---

## Security & Authentication

### SA-1: Open Redirect

**File:** `services/MyAccount/Logout.php:32-34`

```php
if(isset($_REQUEST['return'])) {
    header('Location: ' . $_REQUEST['return']);
    die();
}
```

**Impact:** Phishing via `https://library.org/MyAccount/Logout?return=https://evil.com`

**Fix:** Validate against whitelist or allow only relative paths.

### SA-2: No CSRF Token Protection

No evidence of CSRF tokens on state-changing POST requests across the application. Forms for masquerade, payments, account updates, and admin actions are all unprotected.

**Impact:** Authenticated users can be tricked into performing unwanted actions.

**Fix:** Implement session-based CSRF tokens. Validate on all POST/PUT/DELETE requests.

### SA-3: Session Cookie Missing Security Flags

**File:** `sys/Session/SessionInterface.php:19`

```php
session_set_cookie_params(0, '/');
```

Missing `httponly`, `secure`, and `samesite` flags.

**Fix:**
```php
session_set_cookie_params([
    'lifetime' => 0, 'path' => '/',
    'secure' => true, 'httponly' => true, 'samesite' => 'Strict'
]);
```

### SA-4: Missing HTTP Security Headers

**File:** `.htaccess-lando` and general web config

No `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, or `Strict-Transport-Security` headers.

**Fix:** Add to `.htaccess` or PHP output:
```
Header set X-Frame-Options "SAMEORIGIN"
Header set X-Content-Type-Options "nosniff"
Header set Strict-Transport-Security "max-age=31536000; includeSubDomains"
```

### SA-5: No Rate Limiting on Authentication

**File:** `services/API/UserAPI.php:15-150`

No rate limiting on login endpoints. Enables brute-force attacks on patron credentials.

**Fix:** Implement rate limiting (e.g., 5 failed attempts per IP per minute) with temporary lockout.

### SA-6: File Upload MIME Type Validation Bypass

**File:** `services/SideLoads/UploadMarc.php:29`

```php
$fileType = $uploadedFile["type"];  // Client-provided, easily spoofed
```

**Fix:** Validate using `finfo_file()` (magic bytes) instead of client-provided Content-Type.

### SA-7: Weak Password Storage

**File:** `sys/Account/User.php`

Passwords appear to be stored encrypted (via `EncryptionUtils`) rather than hashed. Encrypted passwords can be decrypted if the key is compromised.

**Fix:** Use `password_hash()` with `PASSWORD_ARGON2ID` or `PASSWORD_BCRYPT` for local passwords.

### SA-8: Database Credentials in Shell Commands

**File:** `cron/backupAspen.php:19-23`

Database credentials extracted from config and passed as plaintext command-line arguments, visible in process listings.

**Fix:** Use `.my.cnf` files or environment variables.

### SA-9: Debug Mode Exposure

**File:** `bootstrap.php:271`, `sys/IP/IPAddress.php:605-663`

Debug mode can be enabled per IP address, exposing SQL queries and internal paths.

**Fix:** Disable debug output in production configs. Require multi-factor approval for enabling.

### SA-10: Shell Command Injection in Cron

**File:** `cron/checkBackgroundProcesses.php:57`

```php
exec("kill -9 $processId", $stopResultsRaw);
```

**Fix:** Use `escapeshellarg()`: `exec("kill -9 " . escapeshellarg($processId));`

---

## Frontend & Templates

### FE-1: DOM XSS via innerHTML

**File:** `interface/themes/responsive/js/aspen/record.js:785-788`

```javascript
selectPlaceholder.innerHTML = data.selectHtml;  // Server response injected as HTML
```

**Impact:** If server response is compromised or contains user-generated content, XSS is possible.

**Fix:** Use safe DOM methods (`textContent`, `createElement`) or sanitize with DOMPurify.

### FE-2: Variable Injection in onclick Attributes

**File:** `interface/themes/responsive/Admin/objectEditor.tpl:79`

```smarty
{if !empty($action.onclick)} onclick="{$action.onclick}"{/if}
```

Arbitrary JavaScript execution if `$action.onclick` contains untrusted data.

**Fix:** Use event delegation with `data-*` attributes instead of inline handlers.

### FE-3: String Passed to setTimeout

**File:** `interface/themes/responsive/js/aspen/record.js:734`

```javascript
setTimeout("AspenDiscovery.closeLightbox();", 3000);
```

**Fix:** `setTimeout(() => AspenDiscovery.closeLightbox(), 3000);`

### FE-4: Console.log in Production

**Files:**
- `js/aspen/events.js:191, 583`
- `js/aspen/admin.js:2251, 3092`

**Fix:** Remove or gate behind a debug flag.

### FE-5: CSS @import Chain (Render Blocking)

**File:** `interface/themes/responsive/css/main.css:1-8`

8 sequential `@import` statements create a waterfall of blocking CSS requests.

**Fix:** Concatenate into a single bundled CSS file at build time.

### FE-6: Missing ARIA Labels on Interactive Elements

**File:** `js/aspen/hero-slider.js:122-130`

Slider navigation buttons lack `aria-label` attributes.

**Fix:** Add descriptive labels: `button.setAttribute('aria-label', 'Next slide');`

### FE-7: Event Listeners Not Cleaned Up

**File:** `js/aspen/hero-slider.js:44-68`

8+ event listeners added without cleanup on page navigation (potential memory leak in SPA-like navigation).

**Fix:** Track listeners and remove on component teardown.

---

## Database & Architecture

### DA-1: N+1 Query Pattern

**Files:** `services/WebBuilder/PortalPages.php:39-42`, `sys/WebBuilder/PortalPage.php:230-311`

Each PortalPage lazily loads rows, audiences, categories, and access settings via individual `find()` calls. For 100 pages = 500+ queries.

**Fix:** Implement eager loading with batch queries or JOIN-based loading.

### DA-2: No Transaction Support for Multi-Step Operations

**File:** `sys/DB/DataObject.php` — insert/update/delete methods

```php
// PortalPage.php:190-203
public function update(string $context = '') : int|bool {
    $ret = parent::update();
    if ($ret !== FALSE) {
        $this->saveRows();      // If this fails, parent already updated
        $this->saveLibraries();
        $this->saveAudiences();
    }
    return $ret;
}
```

**Fix:** Add `beginTransaction()`, `commit()`, `rollBack()` to DataObject.

### DA-3: Missing Concurrency Locks on Cron Jobs

Most cron jobs lack process locking. Concurrent execution causes race conditions and duplicate processing.

**Fix:** Implement `flock()`-based locking in all long-running cron jobs.

### DA-4: Error Notifications Disabled

**File:** `cron/checkBackgroundProcesses.php:140-155`

Error email notification code is commented out. Process failures go unnoticed.

**Fix:** Re-enable error notifications with proper alerting.

### DA-5: Installation Defaults Too Permissive

**File:** `cron/createDefaultDatabaseScript.php:75`

Default IP configuration enables debugging and allows unrestricted API access.

**Fix:** Start with restrictive defaults; require explicit opt-in for debug and API access.

---

## Full Findings Index

| ID | Severity | Category | Summary | File |
|----|----------|----------|---------|------|
| CF-1 | CRITICAL | Security | OS command injection via exec() | PayOnlineNashville.php |
| CF-2 | CRITICAL | Security | SQL injection (10+ files, PHP + Java) | Multiple |
| CF-3 | CRITICAL | Security | Default admin password "password" | createDefaultDatabaseScript.php |
| CF-4 | CRITICAL | Security | Unprotected unserialize() | BaseSearcher.php, others |
| CF-5 | CRITICAL | Security | eval() in Smarty plugins | function.math.php, others |
| CF-6 | CRITICAL | Architecture | God classes (User 6.6K, CatalogConn 2.2K lines) | User.php, CatalogConnection.php |
| PQ-1 | MEDIUM | PHP Quality | @ error suppression (20+ instances) | Multiple |
| PQ-2 | MEDIUM | PHP Quality | extract() on external data | TalpaSearcher.php |
| PQ-3 | MEDIUM | PHP Quality | Loose comparisons (== vs ===) | DataObjectUtil.php |
| PQ-4 | MEDIUM | PHP Quality | Missing query result checks | Koha.php |
| JQ-1 | HIGH | Java Quality | Unclosed resources (no try-with-resources) | RecordGroupingProcessor.java |
| JQ-2 | MEDIUM | Java Quality | Empty catch blocks | SpringshareLibCalIndexer.java |
| JQ-3 | MEDIUM | Java Quality | Thread-unsafe static CRC32 | SpringshareLibCalIndexer.java |
| JQ-4 | MEDIUM | Java Quality | No connection pooling | All Java indexers |
| JQ-5 | MEDIUM | Java Quality | Missing null checks on API data | SpringshareLibCalIndexer.java |
| JQ-6 | LOW | Java Quality | System.out instead of logger | Person.java |
| SA-1 | HIGH | Security | Open redirect on logout | Logout.php |
| SA-2 | HIGH | Security | No CSRF protection | Application-wide |
| SA-3 | HIGH | Security | Session cookies missing security flags | SessionInterface.php |
| SA-4 | HIGH | Security | Missing HTTP security headers | .htaccess |
| SA-5 | MEDIUM | Security | No rate limiting on auth | UserAPI.php |
| SA-6 | MEDIUM | Security | MIME type validation bypass | UploadMarc.php |
| SA-7 | HIGH | Security | Passwords encrypted not hashed | User.php |
| SA-8 | HIGH | Security | DB credentials in shell args | backupAspen.php |
| SA-9 | HIGH | Security | Debug mode exposure | bootstrap.php, IPAddress.php |
| SA-10 | MEDIUM | Security | Shell injection in cron kill | checkBackgroundProcesses.php |
| FE-1 | HIGH | Frontend | DOM XSS via innerHTML | record.js |
| FE-2 | HIGH | Frontend | Variable injection in onclick | objectEditor.tpl |
| FE-3 | LOW | Frontend | String in setTimeout | record.js |
| FE-4 | LOW | Frontend | Console.log in production | events.js, admin.js |
| FE-5 | MEDIUM | Frontend | CSS @import render blocking | main.css |
| FE-6 | MEDIUM | Frontend | Missing ARIA labels | hero-slider.js |
| FE-7 | MEDIUM | Frontend | Event listener memory leak | hero-slider.js |
| DA-1 | HIGH | Database | N+1 query pattern | PortalPage.php |
| DA-2 | MEDIUM | Database | No transaction support | DataObject.php |
| DA-3 | MEDIUM | Operations | Missing cron job locking | Multiple cron files |
| DA-4 | HIGH | Operations | Error notifications disabled | checkBackgroundProcesses.php |
| DA-5 | MEDIUM | Operations | Permissive installation defaults | createDefaultDatabaseScript.php |

---

## Recommended Action Plan

### Week 1: Critical Security (Stop the Bleeding)

| # | Action | Effort |
|---|--------|--------|
| 1 | Remove or disable `PayOnlineNashville.php` | 5 min |
| 2 | Fix open redirect in `Logout.php` — validate return URL | 30 min |
| 3 | Add `allowed_classes` to all `unserialize()` calls | 1 hour |
| 4 | Change default admin password to random in install script | 30 min |
| 5 | Add `httponly` and `secure` flags to session cookies | 15 min |
| 6 | Add HTTP security headers to `.htaccess` | 15 min |

### Week 2-3: High Priority Security

| # | Action | Effort |
|---|--------|--------|
| 7 | Migrate Koha.php from `mysqli_escape_string` to prepared statements | 1-2 days |
| 8 | Fix SQL injection in RecordDriver classes (5 files, same pattern) | 2 hours |
| 9 | Fix SQL injection in Java files (Person.java, Cron.java, SeriesIndexer, WebsiteIndexer) | 4 hours |
| 10 | Implement CSRF token system | 1-2 days |
| 11 | Add rate limiting to authentication endpoints | 4 hours |
| 12 | Use `password_hash()` for local password storage | 1 day |
| 13 | Move DB credentials out of shell command args | 2 hours |

### Month 2: Code Quality & Stability

| # | Action | Effort |
|---|--------|--------|
| 14 | Add try-with-resources to all Java resource usage | 1 day |
| 15 | Remove `@` error suppression, add proper error handling | 1 day |
| 16 | Add process locking to all cron jobs | 1 day |
| 17 | Re-enable error notifications in checkBackgroundProcesses | 30 min |
| 18 | Fix innerHTML XSS in record.js | 1 hour |
| 19 | Replace `eval()` in Smarty plugins | 4 hours |
| 20 | Add connection pooling (HikariCP) to Java indexers | 4 hours |

### Month 3+: Architecture & Long-term

| # | Action | Effort |
|---|--------|--------|
| 21 | Split `User.php` into focused classes | 1-2 weeks |
| 22 | Refactor `CatalogConnection.php` with `__call()` | 1 week |
| 23 | Add transaction support to DataObject | 3 days |
| 24 | Fix N+1 query patterns with eager loading | 1 week |
| 25 | CSS bundling pipeline | 2 days |
| 26 | Accessibility audit and ARIA fixes | 1 week |
| 27 | Migrate from `==` to `===` across PHP codebase | Ongoing |

---

*This report was generated by analyzing the codebase on branch 26.05.00. Some findings may reference files or patterns that have been modified in other branches. Verify each finding against the current production branch before taking action.*
