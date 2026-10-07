module.exports = {
  forbidden: [
    { name: 'no-circular', severity: 'error', from: {}, to: { circular: true } },
    {
      name: 'lib-no-application', severity: 'error',
      from: { path: '^frontend/lib/' },
      to: { path: '^frontend/(components|contexts|features|app)/' },
    },
    {
      name: 'components-no-features', severity: 'error',
      from: { path: '^frontend/components/' }, to: { path: '^frontend/features/' },
    },
    {
      name: 'features-no-routes', severity: 'error',
      from: { path: '^frontend/features/' }, to: { path: '^frontend/app/' },
    },
    {
      name: 'packages-no-application', severity: 'error',
      from: { path: '^(frontend/(shared-ui|packages)/|packages/)' },
      to: { path: '^frontend/', pathNot: '^frontend/(shared-ui|packages|node_modules)/' },
    },
    {
      name: 'no-unresolved-local', severity: 'error', from: {},
      to: { couldNotResolve: true, path: '^(\\.|@/|@puppyone/|frontend/|packages/)' },
    },
  ],
  options: {
    doNotFollow: { path: '(^|/)node_modules/' },
    exclude: { path: '(^|/)(\\.next[^/]*|dist|out|coverage)/|(^|/)next-env\\.d\\.ts$' },
    tsPreCompilationDeps: true,
    tsConfig: { fileName: require('node:path').join(__dirname, 'tsconfig.dependencies.json') },
    enhancedResolveOptions: {
      exportsFields: ['exports'],
      conditionNames: ['import', 'require', 'node', 'default'],
      mainFields: ['module', 'main', 'types'],
    },
  },
};
