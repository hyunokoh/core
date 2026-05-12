// SPDX-License-Identifier: MIT
// Minimal demo ERC20 used by the zkCEX deposit/withdraw simulation.
// `mint` is open to the deployer so the chain bridge can airdrop testnet funds.
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract ZkERC20 is ERC20 {
    address public immutable deployer;
    uint8 private immutable _decimals;

    constructor(string memory name_, string memory symbol_, uint8 decimals_) ERC20(name_, symbol_) {
        deployer = msg.sender;
        _decimals = decimals_;
    }

    function decimals() public view virtual override returns (uint8) {
        return _decimals;
    }

    /// @notice Demo-only mint. The deployer (hardhat dev account[0]) can mint
    /// arbitrary balances to any address. Production tokens would obviously
    /// not expose this.
    function mint(address to, uint256 amount) external {
        require(msg.sender == deployer, "ZkERC20: only deployer can mint");
        _mint(to, amount);
    }
}
