// SPDX-License-Identifier: MIT
// Sample ERC1155 multi-token collection for the zkCEX NFT marketplace demo.
//
// Same minimalism as ZkNFT.sol — owner-only mint, per-id metadata URI, no
// royalties or operator filtering. ERC1155 lets us demonstrate fractional
// editions (one mint can issue N of the same id) alongside the strictly
// non-fungible ERC721 contract.
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC1155/ERC1155.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

contract ZkNFT1155 is ERC1155, Ownable {
    string public name;
    string public symbol;
    uint256 public nextTokenId;
    mapping(uint256 => string) private _tokenURIs;

    event Minted(address indexed to, uint256 indexed tokenId, uint256 amount, string uri);

    constructor(string memory name_, string memory symbol_, address owner_)
        ERC1155("")
        Ownable(owner_)
    {
        name = name_;
        symbol = symbol_;
        nextTokenId = 1;
    }

    /// @notice Mint `amount` of a new id to `to` with the given metadata URI.
    function mint(address to, uint256 amount, string memory uri_)
        external
        onlyOwner
        returns (uint256 tokenId)
    {
        require(amount > 0, "ZkNFT1155: amount=0");
        tokenId = nextTokenId++;
        _tokenURIs[tokenId] = uri_;
        _mint(to, tokenId, amount, "");
        emit Minted(to, tokenId, amount, uri_);
    }

    /// @notice Mint additional units of an existing id.
    function mintMore(address to, uint256 tokenId, uint256 amount) external onlyOwner {
        require(amount > 0, "ZkNFT1155: amount=0");
        require(bytes(_tokenURIs[tokenId]).length > 0, "ZkNFT1155: unknown id");
        _mint(to, tokenId, amount, "");
    }

    function setTokenURI(uint256 tokenId, string memory uri_) external onlyOwner {
        require(bytes(_tokenURIs[tokenId]).length > 0, "ZkNFT1155: unknown id");
        _tokenURIs[tokenId] = uri_;
    }

    function uri(uint256 tokenId) public view virtual override returns (string memory) {
        return _tokenURIs[tokenId];
    }
}
