"""Product commands compile tree splices for NativeOperationWriter.

NativeOperationWriter captures/replays durable requests and delegates ref CAS,
policy, capacity and billing to RefTransactionService. Human and Runtime grants
are checked again at the database publication boundary.
"""
