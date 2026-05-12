// Hardhat config for the zkCEX local-chain simulation.
// chainId 31337 (the hardhat default), hostname/port wired up by run.sh.
require("@nomicfoundation/hardhat-toolbox");

module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      // OpenZeppelin v5 uses MCOPY (introduced in Cancun) inside Arrays.sol
      // and Bytes.sol. The default `paris` EVM version refuses to compile
      // those — set Cancun so both ZkERC20 and the NFT contracts build.
      evmVersion: "cancun",
    },
  },
  networks: {
    hardhat: {
      chainId: 31337,
    },
    localhost: {
      url: "http://127.0.0.1:8545",
      chainId: 31337,
    },
  },
  paths: {
    sources: "./contracts",
    artifacts: "./.local/artifacts",
    cache: "./.local/cache",
  },
};
