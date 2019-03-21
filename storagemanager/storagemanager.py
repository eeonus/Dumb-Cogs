import discord
from discord.ext import commands
from cogs.utils.dataIO import dataIO
from collections import namedtuple, defaultdict, deque
from datetime import datetime
from copy import deepcopy
from .utils import checks
from cogs.utils.chat_formatting import pagify, box
from enum import Enum
import os
import time
import logging
import random

default_settings = {"PAYDAY_TIME": 300, "PAYDAY_CREDITS": 120,
                    "SLOT_MIN": 5, "SLOT_MAX": 100, "SLOT_TIME": 0,
                    "REGISTER_CREDITS": 0}

class EconomyError(Exception):
    pass


class OnCooldown(EconomyError):
    pass


class InvalidBid(EconomyError):
    pass


class BankError(Exception):
    pass


class AccountAlreadyExists(BankError):
    pass


class NoAccount(BankError):
    pass


class InsufficientBalance(BankError):
    pass


class NegativeValue(BankError):
    pass


class SameSenderAndReceiver(BankError):
    pass


NUM_ENC = "\N{COMBINING ENCLOSING KEYCAP}"


class Storage:

    def __init__(self, bot, file_path):
        self.storage = dataIO.load_json(file_path)
        self.bot = bot

    def create_storage(self, user):
        server = user.server
        if not self.storage_exists(user):
            if server.id not in self.storage:
                self.storage[server.id] = {}
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            storage = {"name": user.name,
                       "created_at": timestamp,
                       "items": {}
                       }
            self.storage[server.id][user.id] = storage
            self._save_storage()
            return self.get_storage(user)
        else:
            raise AccountAlreadyExists()

    def storage_exists(self, user):
        try:
            self._get_storage(user)
        except NoAccount:
            return False
        return True

    def withdraw_items(self, user, item, amount):
        server = user.server

        if amount < 0:
            raise NegativeValue()

        storage = self._get_storage(user)
        if storage["items"][item] >= amount:
            storage["items"][item] -= amount
            self.storage[server.id][user.id] = storage
            self._save_storage()
        else:
            raise InsufficientBalance()

    def deposit_items(self, user, item, amount):
        server = user.server
        if amount < 0:
            raise NegativeValue()
        storage = self._get_storage(user)
        if item in storage["items"]:
            storage["items"][item] += amount
        else:
            storage["items"][item] = amount
        self.storage[server.id][user.id] = storage
        self._save_storage()

    def set_items(self, user, item, amount):
        server = user.server
        if amount < 0:
            raise NegativeValue()
        storage = self._get_storage(user)
        storage["items"][item] = amount
        self.storage[server.id][user.id] = storage
        self._save_storage()

    def transfer_items(self, sender, receiver, item, amount):
        if amount < 0:
            raise NegativeValue()
        if sender is receiver:
            raise SameSenderAndReceiver()
        if self.storage_exists(sender) and self.storage_exists(receiver):
            sender_storage = self._get_storage(sender)
            if sender_storage["items"][item] < amount:
                raise InsufficientBalance()
            self.withdraw_items(sender, item, amount)
            self.deposit_items(receiver, item, amount)
        else:
            raise NoAccount()

    def can_spend(self, user, item, amount):
        storage = self._get_storage(user)
        if storage["items"][item] >= amount:
            return True
        else:
            return False

    def wipe_storage(self, server):
        self.storage[server.id] = {}
        self._save_storage()

    def get_server_storage(self, server):
        if server.id in self.storage:
            raw_server_storage = deepcopy(self.storage[server.id])
            storage = []
            for k, v in raw_server_storage.items():
                v["id"] = k
                v["server"] = server
                st = self._create_storage_obj(v)
                storage.append(st)
            return storage
        else:
            return []

    def get_all_storage(self):
        storage = []
        for server_id, v in self.storage.items():
            server = self.bot.get_server(server_id)
            if server is None:
                # Servers that have since been left will be ignored
                # Same for users_id from the old bank format
                continue
            raw_server_storage = deepcopy(self.storage[server.id])
            for k, v in raw_server_storage.items():
                v["id"] = k
                v["server"] = server
                st = self._create_storage_obj(v)
                storage.append(st)
        return storage

    def get_amount(self, user, item):
        storage = self.get_amounts(user)

        if item in storage:
            return storage[item]
        else:
            return 0

    def get_amounts(self, user):
        storage = self._get_storage(user)
        return storage["items"]

    def get_storage(self, user):
        st = self._get_storage(user)
        st["id"] = user.id
        st["server"] = user.server
        return self._create_storage_obj(st)

    def _create_storage_obj(self, storage):
        storage["member"] = storage["server"].get_member(storage["id"])
        storage["created_at"] = datetime.strptime(storage["created_at"],
                                                  "%Y-%m-%d %H:%M:%S")
        storage["items"] = {}
        Storage = namedtuple("Account", "id name items "
                             "created_at server member")
        return Storage(**storage)

    def _save_storage(self):
        dataIO.save_json("data/storagemanager/storage.json", self.storage)

    def _get_storage(self, user):
        server = user.server
        try:
            return deepcopy(self.storage[server.id][user.id])
        except KeyError:
            raise NoAccount()


class SetParser:
    def __init__(self, argument):
        allowed = ("+", "-")
        if argument and argument[0] in allowed:
            try:
                self.sum = int(argument)
            except:
                raise
            if self.sum < 0:
                self.operation = "withdraw"
            elif self.sum > 0:
                self.operation = "deposit"
            else:
                raise
            self.sum = abs(self.sum)
        elif argument.isdigit():
            self.sum = int(argument)
            self.operation = "set"
        else:
            raise


class StorageManager:
    """Economy

    Get rich and have fun with imaginary currency!"""

    def __init__(self, bot):
        global default_settings
        self.bot = bot
        self.storage = Storage(bot, "data/storagemanager/storage.json")
        self.file_path = "data/storagemanager/settings.json"
        self.settings = dataIO.load_json(self.file_path)
        self.settings = defaultdict(default_settings.copy, self.settings)

    @commands.group(name="storage", pass_context=True)
    async def _storage(self, ctx):
        """Bank operations"""
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @_storage.command(pass_context=True, no_pm=True)
    async def register(self, ctx):
        """Registers storage"""
        settings = self.settings[ctx.message.server.id]
        author = ctx.message.author
        try:
            storage = self.storage.create_storage(author)
            await self.bot.say("{} Storage opened."
                               "".format(author.mention))
        except AccountAlreadyExists:
            await self.bot.say("{} You already have a storage"
                               " Twentysix bank.".format(author.mention))

    @_storage.command(pass_context=True)
    async def amount(self, ctx, user: discord.Member, *item):
        """Shows balance of user.

        Defaults to yours."""

        if isinstance(item, tuple):
            item = " ".join(item)

        if not user:
            user = ctx.message.author
            try:
                await self.bot.say("{} Your amount is: {}".format(
                    user.mention, self.storage.get_amount(user, item)))
            except NoAccount:
                await self.bot.say("{} You don't have an account at the"
                                   " Twentysix bank. Type `{}bank register`"
                                   " to open one.".format(user.mention,
                                                          ctx.prefix))
        else:
            try:
                await self.bot.say("{}'s balance is {}".format(
                    user.name, self.storage.get_amount(user, item)))
            except NoAccount:
                await self.bot.say("That user has no bank account.")

    @_storage.command(pass_context=True)
    async def amounts(self, ctx, user: discord.Member):
        """Shows balance of user.

        Defaults to yours."""


        if not user:
            user = ctx.message.author

        try:
            storage = self.storage.get_amounts(user)
            lines = ["{}'s quantities are".format(user.name)]

            for item in sorted(storage.keys()):
                lines.append('  {0}: {1}'.format(item, storage[item]))

            await self.bot.say("```" + "\n".join(lines) + "```")
        except NoAccount:
            await self.bot.say("That user storage does not exist.")

    @_storage.command(pass_context=True)
    async def transfer(self, ctx, user: discord.Member, sum:int, *item):
        """Transfer credits to other users"""
        author = ctx.message.author

        if isinstance(item, tuple):
            item = " ".join(item)

        try:
            self.storage.transfer_items(author, user, item, sum)
            logger.info("{}({}) transferred {} {} to {}({})".format(
                author.name, author.id, sum, item, user.name, user.id))
            await self.bot.say("{} credits have been transferred to {}'s"
                               " account.".format(sum, user.name))
        except NegativeValue:
            await self.bot.say("You need to transfer at least 1 item.")
        except SameSenderAndReceiver:
            await self.bot.say("You can't transfer credits to yourself.")
        except InsufficientBalance:
            await self.bot.say("You don't have that sum in your bank account.")
        except NoAccount:
            await self.bot.say("That user has no bank account.")

    # @_storage.command(name="set", pass_context=True)
    # @checks.admin_or_permissions(manage_server=True)
    # async def _set(self, ctx, user: discord.Member, item, amount):
    #     """Sets credits of user's bank account. See help for more operations
    #
    #     Passing positive and negative values will add/remove credits instead
    #
    #     Examples:
    #         bank set @Twentysix 26 - Sets 26 credits
    #         bank set @Twentysix +2 - Adds 2 credits
    #         bank set @Twentysix -6 - Removes 6 credits"""
    #     author = ctx.message.author
    #     try:
    #         if credits.operation == "deposit":
    #             self.bank.deposit_credits(user, credits.sum)
    #             logger.info("{}({}) added {} credits to {} ({})".format(
    #                 author.name, author.id, credits.sum, user.name, user.id))
    #             await self.bot.say("{} credits have been added to {}"
    #                                "".format(credits.sum, user.name))
    #         elif credits.operation == "withdraw":
    #             self.bank.withdraw_credits(user, credits.sum)
    #             logger.info("{}({}) removed {} credits to {} ({})".format(
    #                 author.name, author.id, credits.sum, user.name, user.id))
    #             await self.bot.say("{} credits have been withdrawn from {}"
    #                                "".format(credits.sum, user.name))
    #         elif credits.operation == "set":
    #             self.bank.set_credits(user, credits.sum)
    #             logger.info("{}({}) set {} credits to {} ({})"
    #                         "".format(author.name, author.id, credits.sum,
    #                                   user.name, user.id))
    #             await self.bot.say("{}'s credits have been set to {}".format(
    #                 user.name, credits.sum))
    #     except InsufficientBalance:
    #         await self.bot.say("User doesn't have enough credits.")
    #     except NoAccount:
    #         await self.bot.say("User has no bank account.")

    @_storage.command(pass_context=True, no_pm=True)
    @checks.serverowner_or_permissions(administrator=True)
    async def reset(self, ctx, confirmation: bool=False):
        """Deletes all server's bank accounts"""
        if confirmation is False:
            await self.bot.say("This will delete all bank accounts on "
                               "this server.\nIf you're sure, type "
                               "{}bank reset yes".format(ctx.prefix))
        else:
            self.storage.wipe_storage(ctx.message.server)
            await self.bot.say("All bank accounts of this server have been "
                               "deleted.")

    def already_in_list(self, accounts, user):
        for st in storage:
            if user.id == st.id:
                return True
        return False

    @commands.group(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def storageset(self, ctx):
        """Changes economy module settings"""
        server = ctx.message.server
        settings = self.settings[server.id]
        if ctx.invoked_subcommand is None:
            msg = "```"
            for k, v in settings.items():
                msg += "{}: {}\n".format(k, v)
            msg += "```"
            await self.bot.send_cmd_help(ctx)
            await self.bot.say(msg)

    # What would I ever do without stackoverflow?
    def display_time(self, seconds, granularity=2):
        intervals = (  # Source: http://stackoverflow.com/a/24542445
            ('weeks', 604800),  # 60 * 60 * 24 * 7
            ('days', 86400),    # 60 * 60 * 24
            ('hours', 3600),    # 60 * 60
            ('minutes', 60),
            ('seconds', 1),
        )

        result = []

        for name, count in intervals:
            value = seconds // count
            if value:
                seconds -= value * count
                if value == 1:
                    name = name.rstrip('s')
                result.append("{} {}".format(value, name))
        return ', '.join(result[:granularity])


def check_folders():
    if not os.path.exists("data/economy"):
        print("Creating data/economy folder...")
        os.makedirs("data/economy")


def check_files():

    f = "data/storagemanager/settings.json"
    if not dataIO.is_valid_json(f):
        print("Creating default economy's settings.json...")
        dataIO.save_json(f, {})

    f = "data/storagemanager/storage.json"
    if not dataIO.is_valid_json(f):
        print("Creating empty storage.json...")
        dataIO.save_json(f, {})


def setup(bot):
    global logger
    check_folders()
    check_files()
    logger = logging.getLogger("red.economy")
    if logger.level == 0:
        # Prevents the logger from being loaded again in case of module reload
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(
            filename='data/economy/storage.log', encoding='utf-8', mode='a')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(message)s', datefmt="[%d/%m/%Y %H:%M]"))
        logger.addHandler(handler)
    bot.add_cog(StorageManager(bot))
