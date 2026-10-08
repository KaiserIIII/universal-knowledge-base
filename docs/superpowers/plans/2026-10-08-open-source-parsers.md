# Zhixu open-source conversion and parser modules

Approved scope: free, open-source, self-hosted deployment; unlimited resource counts;
preserve tenant isolation, roles and existing knowledge. Keep the original release
branch's application code and add bilingual documentation and Apache-2.0 licensing.

1. Remove commercial routes, adapters, navigation, settings and tests.
2. Migrate the old workflow foreign-key requirement transactionally, preserving
   historical rows, columns, indexes and legacy accounting tables.
3. Expose a local parser catalog and per-library parsing settings. Use existing
   bounded Office/PDF extraction plus structured CSV/TSV and encoding selection.
4. Add a typed file-evidence workflow module using tenant-scoped, completed SQL
   documents. Recheck scope at execution; never accept arbitrary server paths.
5. Refresh both language versions, architecture and deployment documentation,
   reference provenance, and workflow screenshots. Disable retrieval telemetry.
6. Run migration/parser regressions, complete API and browser suites, syntax and
   residual checks. Review the final diff and publish one main commit; retain only
   main and the original universal-knowledge-base branch remotely.

Reference reviewed: KaiserIIII/langbot-unified-attachment-pipeline,
commit 3c7ea58a67a9a30bc0c92738b8f9bcde63a34464, Apache-2.0, 2026-10-08.
Design reference only; no library installation or copied executable source.
