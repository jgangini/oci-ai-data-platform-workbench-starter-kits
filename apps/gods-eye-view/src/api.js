// The installed admin proxy may precede the viewer update; retain its authenticated
// wire route for fetch and image requests without probing or retrying mutations.
export function viewerApiPath(path) {
  return path.replace(/^\/api\/gods-eye-view\//, '/api/prisma/');
}
