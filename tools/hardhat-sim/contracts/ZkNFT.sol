// SPDX-License-Identifier: MIT
// Sample ERC721 collection used by the zkCEX NFT marketplace demo.
//
// Deliberately minimal:
//   * Owner is the deployer; only the owner can mint.
//   * The custodial address holds the initial supply (just like ZETH/ZUSDT)
//     so the demo can hand tokens to users without per-user signing.
//   * Per-token metadata URI is set at mint time. We use http(s) URLs that
//     point at JSON files served by the homepage so the browser can render
//     thumbnails without IPFS.
//
// Not a production design — no royalties, no permits, no operator allowance
// management beyond the OZ default. The marketplace orchestrates transfers
// from the custodial address, which holds approval for itself by being
// the token holder.
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

contract ZkNFT is ERC721, Ownable {
    uint256 public nextTokenId;
    mapping(uint256 => string) private _tokenURIs;

    event Minted(address indexed to, uint256 indexed tokenId, string uri);

    constructor(string memory name_, string memory symbol_, address owner_)
        ERC721(name_, symbol_)
        Ownable(owner_)
    {
        nextTokenId = 1;
    }

    /// @notice Mint a new token to `to` with the given metadata URI.
    ///         Only callable by the contract owner (hardhat dev account[0]).
    function mint(address to, string memory uri) external onlyOwner returns (uint256 tokenId) {
        tokenId = nextTokenId++;
        _safeMint(to, tokenId);
        _tokenURIs[tokenId] = uri;
        emit Minted(to, tokenId, uri);
    }

    /// @notice Set or update the metadata URI for an existing token.
    function setTokenURI(uint256 tokenId, string memory uri) external onlyOwner {
        _requireOwned(tokenId);
        _tokenURIs[tokenId] = uri;
    }

    function tokenURI(uint256 tokenId) public view virtual override returns (string memory) {
        _requireOwned(tokenId);
        return _tokenURIs[tokenId];
    }

    /// @notice Total minted so far (not strictly EIP-721 but handy for the UI).
    function totalSupply() external view returns (uint256) {
        return nextTokenId - 1;
    }
}
