# Simon and RobbinsHome boundaries

Simon owns AI conversations, account-scoped memory and connections, native projects,
boards and scoped staffing. RobbinsHome owns physical devices, home automation,
printers and device command receipts in its separate service and database.

The optional integration uses [HomeClient](../../src/simon/adapters/home_client.py)
with `SIMON_HOME_API_URL` and `SIMON_HOME_API_TOKEN`. There are no cross-repository
Python imports or shared database reads. Simon revalidates the requesting account
before bounded API calls; upstream access is determined by the external service's
token grants. RobbinsHome's `household_id` wire field is its API contract, not a
second Simon tenant identifier. Simon identity input requires `workspace_id`.

External outages do not disable unrelated assistant or native project features.
Installing this source does not migrate device data, restart external services,
transfer credentials, or register a compatibility worker. Deployment of either
service needs its own explicit operational acceptance and recovery procedure.
