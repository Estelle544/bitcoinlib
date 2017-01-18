# -*- coding: utf-8 -*-
#
#    bitcoinlib Transactions
#    © 2017 January - 1200 Web Development <http://1200wd.com/>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import binascii
import hashlib
from bitcoinlib.encoding import *
from bitcoinlib.config.opcodes import opcodes, opcodenames
from bitcoinlib.keys import Key
from bitcoinlib.main import *
from bitcoinlib.services.bitcoind import BitcoindClient

_logger = logging.getLogger(__name__)


class TransactionError(Exception):
    def __init__(self, msg=''):
        self.msg = msg
        _logger.error(msg)

    def __str__(self):
        return self.msg


def deserialize_transaction(rawtx):
    """
    Deserialize a raw transaction

    :param rawtx: Raw transaction in bytes
    :return: json list with inputs, outputs, locktime and version
    """
    version = rawtx[0:4][::-1]
    n_inputs, size = varbyteint_to_int(rawtx[4:13])
    cursor = 4 + size
    inputs = []
    for i in range(0, n_inputs):
        inp_hash = rawtx[cursor:cursor + 32][::-1]
        if not len(inp_hash):
            raise TransactionError("Input transaction hash not found. Probably malformed raw transaction")
        inp_index = rawtx[cursor + 32:cursor + 36][::-1]
        cursor += 36

        scriptsig_size, size = varbyteint_to_int(rawtx[cursor:cursor + 9])
        cursor += size
        scriptsig = rawtx[cursor:cursor + scriptsig_size]
        cursor += scriptsig_size
        sequence_number = rawtx[cursor:cursor + 4]
        cursor += 4
        inputs.append(Input(inp_hash, inp_index, scriptsig, sequence_number, i))
    if len(inputs) != n_inputs:
        raise TransactionError("Error parsing inputs. Number of tx specified %d but %d found" % (n_inputs, len(inputs)))

    outputs = []
    n_outputs, size = varbyteint_to_int(rawtx[cursor:cursor + 9])
    cursor += size
    for o in range(0, n_outputs):
        amount = change_base(rawtx[cursor:cursor + 8][::-1], 256, 10)
        cursor += 8
        script_size, size = varbyteint_to_int(rawtx[cursor:cursor + 9])
        cursor += size
        script = rawtx[cursor:cursor + script_size]
        cursor += script_size
        outputs.append({'amount': amount, 'script': script, })
    if not outputs:
        raise TransactionError("Error no outputs found in this transaction")
    locktime = change_base(rawtx[cursor:cursor + 4][::-1], 256, 10)

    return inputs, outputs, locktime, version


def parse_script_sig(s):
    l = s[0]
    sig = convert_der_sig(s[1:l])
    l2 = s[l+1]
    public_key = s[l+2:l+l2+2]
    return sig, public_key


class Input:

    def __init__(self, prev_hash, output_index, script_sig, sequence, id=0):
        self.id = id
        self.prev_hash = prev_hash
        self.output_index = output_index
        self.script_sig = script_sig
        self.sequence = sequence

    def json(self):
        return {
            'prev_hash': binascii.hexlify(self.prev_hash).decode('utf-8'),
            'output_index': binascii.hexlify(self.output_index).decode('utf-8'),
            'script_sig': binascii.hexlify(self.script_sig).decode('utf-8'),
            'sequence': binascii.hexlify(self.sequence).decode('utf-8'),
        }

    def __repr__(self):
        return str(self.json())


class Transaction:

    @staticmethod
    def import_raw(rawtx):
        if isinstance(rawtx, str):
            rawtx = binascii.unhexlify(rawtx)
        elif not isinstance(rawtx, bytes):
            raise TransactionError("Raw Transaction must be of type bytes or str")

        inputs, outputs, locktime, version = deserialize_transaction(rawtx)

        return Transaction(inputs, outputs, locktime, version)

    def __init__(self, inputs, outputs, locktime=0, version=b'00000001'):
        self.inputs = inputs
        self.outputs = outputs
        self.version = version
        self.locktime = locktime

    def input_addresses(self, id=None, return_type='hash160'):
        r = []
        for i in self.inputs:
            s = i.script_sig
            l = s[0]
            sig_der = s[1:l]
            l2 = s[l+1]
            public_key = binascii.hexlify(s[l+2:l+l2+2]).decode('utf-8')
            k = Key(public_key, compressed=False)
            if return_type == 'hash160':
                r.append(k.hash160())
            elif return_type == 'hex':
                r.append(k.public_uncompressed())
            elif return_type == 'bytes':
                r.append(binascii.unhexlify(k.public_uncompressed()))
        if id is None:
            return r
        else:
            return r[id]

    def get(self):
        inputs = []
        for i in self.inputs:
            inputs.append(i.json())
        return {
            'inputs': inputs,
            'outputs': self.outputs,
            'locktime': self.locktime,
        }

    def raw(self, sign_id=None):
        r = self.version[::-1]
        r += int_to_varbyteint(len(self.inputs))
        for i in self.inputs:
            r += i.prev_hash[::-1] + i.output_index[::-1]
            if sign_id is None:
                r += struct.pack('B', len(i.script_sig)) + i.script_sig
            elif sign_id == i.id:
                r += b'\x19\x76\xa9\x14' + binascii.unhexlify(self.input_addresses(id=i.id)) + \
                     b'\x88\xac'
            else:
                r += b'\0'
            r += i.sequence

        r += int_to_varbyteint(len(self.outputs))
        for o in self.outputs:
            r += struct.pack('<Q', o['amount'])
            r += struct.pack('B', len(o['script'])) + o['script']
        r += struct.pack('<L', self.locktime)
        if sign_id is not None:
            r += b'\1\0\0\0'
        return r

    def verify(self):
        for i in self.inputs:
            t_to_sign = self.raw(i.id)
            hashtosign = hashlib.sha256(hashlib.sha256(t_to_sign).digest()).digest()
            signature, pub_key = parse_script_sig(i.script_sig)
            if len(pub_key) == 33:
                pub_key = binascii.unhexlify(Key(binascii.hexlify(pub_key).decode('utf-8')).public_uncompressed())
            vk = ecdsa.VerifyingKey.from_string(pub_key[1:], curve=ecdsa.SECP256k1)
            try:
                vk.verify_digest(binascii.unhexlify(signature), hashtosign)
            except:
                _logger.info("Bad Signature %s" % signature)
                return False
            _logger.info("Signature Verified %s" % signature)
        return True


if __name__ == '__main__':
    from pprint import pprint

    # verified ok, signature 1f6e18f4532e14f328bc820cb78c53c57c91b1da9949fecb8cf42318b791fb3845e78c9e55df1cf3db74bfd52ff2add2b59ba63e068680f0023e6a80ac9f51f4
    # Example of a basic raw transaction with 1 input and 2 outputs
    # (destination and change address).
    rt =  '01000000'  # Version bytes in Little-Endian (reversed) format
    # --- INPUTS ---
    rt += '01'        # Number of UTXO's inputs
    # Previous transaction hash (Little Endian format):
    rt += 'a3919372c9807d92507289d71bdd38f10682a49c47e50dc0136996b43d8aa54e'
    rt += '01000000'  # Index number of previous transaction
    # - INPUT: SCRIPTSIG -
    rt += '6a'        # Size of following unlocking script (ScripSig)
    rt += '47'        # PUSHDATA 47 - Push following 47 bytes signature to stack
    rt += '30'        # DER encoded Signature - Sequence
    rt += '44'        # DER encoded Signature - Length
    rt += '02'        # DER encoded Signature - Integer
    rt += '20'        # DER encoded Signature - Length of X:
    rt += '1f6e18f4532e14f328bc820cb78c53c57c91b1da9949fecb8cf42318b791fb38'
    rt += '02'        # DER encoded Signature - Integer
    rt += '20'        # DER encoded Signature - Lenght of Y:
    rt += '45e78c9e55df1cf3db74bfd52ff2add2b59ba63e068680f0023e6a80ac9f51f4'
    rt += '01'        # SIGHASH_ALL
    # - INPUT: PUBLIC KEY -
    rt += '21'        # PUSHDATA 21 - Push following 21 bytes public key to stack:
    rt += '0239a18d586c34e51238a7c9a27a342abfb35e3e4aa5ac6559889db1dab2816e9d'
    rt += 'feffffff'  # Sequence
    # --- OUTPUTS ---
    rt += '02'                  # Number of outputs
    rt += '3ef5980400000000'    # Output value in Little Endian format
    rt += '19'                  # Script length, of following scriptPubKey:
    rt += '76a914af8e14a2cecd715c363b3a72b55b59a31e2acac988ac'
    rt += '90940d0000000000'    # Output value #2 in Little Endian format
    rt += '19'                  # Script length, of following scriptPubKey:
    rt += '76a914f0d34949650af161e7cb3f0325a1a8833075165088ac'
    rt += 'b7740f00'   # Locktime

    # Verified ok, sig = 2c2e1a746c556546f2c959e92f2d0bd2678274823cc55e11628284e4a13016f8797e716835f9dbcddb752cd0115a970a022ea6f2d8edafff6e087f928e41baac
    # rt = (
    # "0100000001a97830933769fe33c6155286ffae34db44c6b8783a2d8ca52ebee6414d399ec300000000" + "8a47" + "304402202c2e1a746c556546f2c959e92f2d0bd2678274823cc55e11628284e4a13016f80220797e716835f9dbcddb752cd0115a970a022ea6f2d8edafff6e087f928e41baac01" + "41" + "04392b964e911955ed50e4e368a9476bc3f9dcc134280e15636430eb91145dab739f0d68b82cf33003379d885a0b212ac95e9cddfd2d391807934d25995468bc55" + "ffffffff02015f0000000000001976a914c8e90996c7c6080ee06284600c684ed904d14c5c88ac204e000000000000" + "1976a914348514b329fda7bd33c7b2336cf7cd1fc9544c0588ac00000000")

    # rt = '0100000004be8a976420ef000956142320e79d90dd2ce103dda9cf51efb280468ca7ac121d000000006b483045022100e80841d3a21a12c505e60d2896631edac06e0e0e7359207583cb31dd490a652502204fde02010706097f11acd0547c9dff0399354c065d7e1d1d17eeda031185804c0121029418397b2ad61b6d603fc865eb4ada9c5425952c4dbe948a0e0c75c36d4e740affffffffc4475d1a9a50aae5c608d20c28a1ca78bda39056d22aa3d869aefbdab83aa4b4000000006b483045022100cd986b35450080a2ee9397349d7513cecff5cf56c435cae43d33ca83c69cddb30220259f9460b372025dff475a534c472c3b2b7f558f393aedeb4c2a30fb6156f81c01210316dec74bb3f916cab37a979c076e03b54f347fa5a90bf2fc9f14e435c1a4ecbdffffffffaea58d46919cf6b7641a30a0a027f3318aee9173fc3f8f1f03c39670f7ce5c3a000000006a47304402206b3297db37c68ae172dc0de46cdb165ec79ce491edec7d59ed98c80d82edeffb0220244665fec2da49eae564d4cc78939ae2c04504294bbca76367d2e9ce5802f56d0121035b5ff8a770e99152d210f1d875d0e1c570dc9fbe332eaecfc405254f6df59edcffffffff85778efe6c0347762b404a6b5b00c45e7143861ccb2b4bd7b0927d0db9fee509010000006a473044022045330b90adba441e797350baa8a631c3b0d375598c88d6eaaae74526698a7fdc022066ffb7a61fcd394d8eed953eac5a792eccddb20f7b14f4e8dcbdc4e9207f1d1c0121032ebd92c614095f612a9e0dbcdb0d03e75481f9335c756f17bfc206d0dcddc644ffffffff02ce8fb400000000001976a914377ad7e288e893dc4473aeb28b18b1675067abaf88aca4823e00000000001976a914bfb2eb5487e238c7d34ea12b965ae169fba563ba88ac00000000'

    # new from the block
    # rt ='01000000027feae018535b4e8c4085842c8b1231e028a337f7479faf2223e492834561bc46010000006a47304402204093fdf0bda73daa6c9adb9c263ac2f2dc2bb5345a42e7d84ac4df5c56f9101402200a6d32cd9fa49e2ffccb6a45ff11ffdbdb2b6726f837f39889dc1a619259a313012102aede1735e06837692bd3ecb1cbc4f09f8f47d39138b92f5a39fdd1064cea9754ffffffff5d93cd125e1c1b032e49f86cfa1a6eef079110bc82bcedaba772bef6409f2c70010000006a47304402207afde02ff15c7011b003f42da7d5e566a11913e928c6dc8b1cbf0e5fb404073202202dcf97f4e0844c34abd676bf86e3df0b3e6261a1af5dae8944e99db8b2c9cd25012103b1dbc92fc9ab32fc9311eca4f8f64c8cc1bf08ba1581b76061cc4d1f5594c95fffffffff0260583daf000000001976a9141064198f6ac88004252c1a326a4e3ef62f40407188ac207154380b0000001976a914779ed60f20aed94a1134f2bf35d990935e83561288ac00000000'

    # verified ok, sig: 73a1f75574f6619b75fe0e00fc020b6293a0a47509e3b616d746f7f6d24ed14e50e04004d2cb6768d3f7d47f17bb
    # 4f9b1eac3503760f029cd84d2cc418e90a24
    # ssig = 473044022073a1f75574f6619b75fe0e00fc020b6293a0a47509e3b616d746f7f6d24ed14e022050e04004d2cb6768d3f7d47f1
    # 7bb4f9b1eac3503760f029cd84d2cc418e90a2401210245377a30fc048b5ffa8a772fda927605b25313dec255892bcc625f09c5c32286
    # rt = '01000000014c428a09c84ed161bace114ee75e8c4067c688b8c6f5a4088b214644cb180cf1010000006a473044022073a1f75574f6619b75fe0e00fc020b6293a0a47509e3b616d746f7f6d24ed14e022050e04004d2cb6768d3f7d47f17bb4f9b1eac3503760f029cd84d2cc418e90a2401210245377a30fc048b5ffa8a772fda927605b25313dec255892bcc625f09c5c32286ffffffff02400d0300000000001976a91400264935f054ea1848a3f773df5a05682906188688aca066e80b000000001976a9145072694f9d4b01121070ca7345da8a38fa25fb7888ac00000000'


    t = Transaction.import_raw(rt)
    print("raw %s" % binascii.hexlify(t.raw()))
    pprint(t.get())
    print("Verified %s" % t.verify())

    if False:  # Set to True to enable example
        # Deserialize transactions in latest block with bitcoind client
        bdc = BitcoindClient.from_config()

        print("\n=== DESERIALIZE LAST BLOCKS TRANSACTIONS ===")
        blockhash = bdc.proxy.getbestblockhash()
        bestblock = bdc.proxy.getblock(blockhash)
        print('... %d transactions found' % len(bestblock['tx']))
        ci = 0
        ct = len(bestblock['tx'])
        for txid in bestblock['tx']:
            ci += 1
            print("[%d/%d] Deserialize txid %s" % (ci, ct, txid))
            try:
                rt = bdc.getrawtransaction(txid)
            except:
                pass
            print("- raw %s" % rt)
            t = Transaction.import_raw(rt)
            pprint(t.get())
        print("===   %d raw transactions deserialised   ===" % ct)
        print("===   D O N E   ===")

        # Deserialize transactions in the bitcoind mempool client
        print("\n=== DESERIALIZE MEMPOOL TRANSACTIONS ===")
        newtxs = bdc.proxy.getrawmempool()
        ci = 0
        ct = len(newtxs)
        print("Found %d transactions in mempool" % len(newtxs))
        for txid in newtxs:
            ci += 1
            print("[%d/%d] Deserialize txid %s" % (ci, ct, txid))
            try:
                rt = bdc.getrawtransaction(txid)
                print("- raw %s" % rt)
                t = Transaction.import_raw(rt)
                pprint(t.get())
            except:
                print(txid)
        print("===   %d mempool transactions deserialised   ===" % ct)
        print("===   D O N E   ===")
