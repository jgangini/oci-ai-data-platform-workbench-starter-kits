// Report actual provider completions, preserving Cesium's throttling and retry behavior.
export function observeImagery(provider, onState) {
  const request = provider.requestImage.bind(provider);
  const state = { loaded: 0, failed: 0 };
  const failedTiles = new Set();
  provider.requestImage = (...args) => {
    const pending = request(...args);
    if (!pending) return pending;
    const tile = args.slice(0, 3).join('/');
    return Promise.resolve(pending).then((image) => {
      failedTiles.delete(tile); state.failed = failedTiles.size;
      state.loaded += 1; onState({ ...state }); return image;
    }, (error) => {
      failedTiles.add(tile); state.failed = failedTiles.size;
      onState({ ...state }); throw error;
    });
  };
}
