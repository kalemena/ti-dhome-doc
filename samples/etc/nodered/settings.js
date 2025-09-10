// Node-RED runtime settings, mounted read-only at /data/settings.js.
//
// Security values are injected as container environment variables from
// workspace/nodered.env (see `make env-secret` and `make password.nodered`):
//   NODE_RED_CREDENTIAL_SECRET  encrypts flow credentials on disk
//   NODE_RED_ADMIN_HASH         bcrypt hash of the admin password
//
// Everything not set here keeps Node-RED's built-in defaults. See
// https://nodered.org/docs/user-guide/runtime/securing-node-red

module.exports = {
    // Encrypts the credentials stored in flows_cred.json (broker passwords,
    // API keys, ...). Do not change once sets are deployed: existing flow
    // credentials would no longer be decryptable.
    credentialSecret: process.env.NODE_RED_CREDENTIAL_SECRET,

    // Protects the editor and the admin API. Fails closed: if the hash is not
    // set (empty env), logins are rejected until `make password.nodered` runs.
    adminAuth: {
        type: "credentials",
        users: [{
            username: "admin",
            password: process.env.NODE_RED_ADMIN_HASH,
            permissions: "*"
        }]
    },

    // Protects HTTP endpoints created by http nodes (exposed through Caddy).
    // Uses the same hash as the editor.
    httpNodeAuth: { user: "admin", pass: process.env.NODE_RED_ADMIN_HASH }
};