# English Interface Implementation

**Goal:** Complete the approved knowledge workspace interface with a persisted Chinese/English selector and publish the verified update before merging the existing PR.

**Architecture:** Keep the local ESM frontend and API unchanged. A shared dictionary and explicit static-template translation helpers localize application copy while preserving interpolated business data. A confirmed page reload applies the selected language and retains the route; a validated URL lang parameter also works when storage is unavailable.

**Tech Stack:** Browser ESM, vanilla JavaScript, CSS, Node built-in tests; no new runtime dependencies.

## Global Constraints

- Locales are `zh-CN` and `en`; default is Chinese. Persist preference safely and support validated URL selection.
- Translate login/registration, navigation, nine application views, dialogs, help, statuses, friendly errors, dates and API reference.
- Translate static UI copy only. Keep interpolated organization names, document contents, queries, answers, sources, custom labels, saved prompts and workflow JSON unchanged. Keep API fields/IDs/enums/protocols unchanged.
- Require explicit confirmation before locale reload; cancellation leaves preference and navigation unchanged. Warn about unsaved edits and requests in progress. Preserve the hash route.
- Do not use global DOM string replacement or remote translation. Do not introduce runtime dependencies. Do not read private environments or datasets.
- Only frontend/tests/docs belong to this increment. Original checkout changes remain untouched. Existing PR head before this increment is `4041095e917ad40de55735c7d6b57df1df30c5c4`.

### Task 1: Complete bilingual frontend

**Files:**
- Create: `web/i18n.js`, local dictionary module(s), `tests/web/i18n.test.mjs`
- Modify: `index.html`, `web/api-docs.html`, `web/api-docs.js`, `web/app.js`, `web/dom.js`, `web/api.js`, `web/views.js`, `web/knowledge.js`, `web/chat.js`, `web/evaluation.js`, `web/models.js`, `web/canvas.js`, `web/workflows.js`, `web/graph.js`, `web/workflow-run.js`, `web/styles.css`
- Modify existing Node test fixtures only to make locale explicit where behavior assertions require a particular language.

**Interfaces:**
- Consume the existing escaped HTML templates, API contracts and view render/cleanup lifecycle.
- Produce local `t`, `html`, locale getter/setter and language-control binding APIs, documented in the implementation report. No backend change is required.

- [ ] Write an actual-render failing regression selecting English and require visible English navigation/form/help. Use existing synthetic DOM fixtures or a small equivalent. Also require a Chinese organization/document/message supplied as API data to remain byte-for-byte unchanged in the resulting markup.

```js
assert.match(navigation.innerHTML, /Visual workflows/);
assert.match(renderedKnowledge, /Import documents/);
assert.ok(renderedKnowledge.includes('中文合成知识库'));
```

- [ ] Run the focused tests and record their expected RED failure before writing production code.
- [ ] Implement preference/URL validation and shared translation helpers. Test static fragments separately from values:

```js
setLocale('en');
assert.equal(t('知识库'), 'Knowledge bases');
assert.ok(html`<h2>知识库</h2><p>${'知识库：中文合成资料'}</p>`
  .includes('<p>知识库：中文合成资料</p>'));
```

- [ ] Localize every listed view and static page, preserving all user-controlled values and API/config data. Keep unknown errors as received. Localize date formatting and safe error messages.
- [ ] Add the selector and confirmed reload, including unavailable-storage URL fallback, cancel-without-write and current-hash preservation tests. Keep existing interaction and scope-race regressions meaningful.
- [ ] Run `node --test tests/web/*.test.mjs`, syntax-check every `web/*.js`, and `git diff --check`. Write exact commands/results and remaining concerns in the task report. Stage only owned frontend/tests and commit the completed task.

### Root integration and publication

- [ ] Independently review Task 1 against the full constraints; resolve material findings through a scoped fix/review loop.
- [ ] In the existing synthetic preview verify login and the complete English navigation, workflows and parameter inspector; verify Chinese switching, user content preservation and 390px overflow. Preserve a synthetic screenshot only.
- [ ] Update README/README_zh and verification evidence with actual findings, then review the complete increment relative to `4041095e`.
- [ ] Publish through local git or the authorized GitHub API fallback, verifying the remote tree equals the tested local tree. Update/attach PR #1, wait for latest Windows/Linux CI, mark ready and merge using the expected final head SHA.
- [ ] Archive local review evidence. The prior automatic refusal of recursive temporary-directory deletion remains in force; retain ignored scratch rather than retrying that action through another path.
