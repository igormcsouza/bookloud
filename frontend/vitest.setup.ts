import "@testing-library/jest-dom/vitest";

// jsdom implements no layout, so `Element.prototype.scrollIntoView` simply
// does not exist -- ChunkParagraph's follow-along scroll would throw on every
// render of an active paragraph. Stubbed globally rather than guarded in the
// component, because "does this environment do layout?" is a test-environment
// fact, not a thing the reader should carry a branch for.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}
