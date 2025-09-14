from typing import Any, Dict, List

class TransactionData:
    hash: str

class ReceiptOutcome:
    gas_burnt: int

class TransactionResult:
    status: Dict[str, Any]
    transaction: TransactionData
    transaction_outcome: ReceiptOutcome
    @property
    def logs(self) -> List[str]: ...
