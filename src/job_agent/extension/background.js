// Keep pairing credentials restricted to extension-owned contexts.
chrome.storage.local.setAccessLevel?.({accessLevel: "TRUSTED_CONTEXTS"}).catch(() => {});
